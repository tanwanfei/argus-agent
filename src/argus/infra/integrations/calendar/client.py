import asyncio
import logging
import re
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, tzinfo
from typing import Final
from urllib.parse import urljoin

import httpx2
import icalendar
import recurring_ical_events

from argus.kernel.tools import ToolFailure

logger = logging.getLogger(__name__)

_TIMEOUT = httpx2.Timeout(10.0)

_DAV: Final = "{DAV:}"
_CALDAV: Final = "{urn:ietf:params:xml:ns:caldav}"

_NAMESPACES = 'xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"'
_FIND_HOME_BODY = f"""<d:propfind {_NAMESPACES}>
  <d:prop><d:current-user-principal/><c:calendar-home-set/></d:prop>
</d:propfind>"""
_LIST_CALENDARS_BODY = f"""<d:propfind {_NAMESPACES}>
  <d:prop><d:resourcetype/><d:displayname/><c:supported-calendar-component-set/></d:prop>
</d:propfind>"""


@dataclass(frozen=True)
class CalendarEvent:
    uid: str
    title: str
    start: date | datetime
    end: date | datetime
    location: str | None


@dataclass(frozen=True)
class AddedEvent:
    url: str
    calendar_name: str


@dataclass(frozen=True)
class _Calendar:
    url: str
    name: str


@dataclass(frozen=True)
class _DavResponse:
    url: str
    properties: dict[str, ElementTree.Element]


class CalDavClient:
    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url
        self._client = httpx2.AsyncClient(
            auth=httpx2.BasicAuth(username, password),
            trust_env=False,
            timeout=_TIMEOUT,
            transport=transport,
        )
        self._calendars: list[_Calendar] | None = None

    async def events_between(self, start: datetime, end: datetime) -> list[CalendarEvent]:
        zone = start.tzinfo
        if zone is None or end.tzinfo is None:
            raise ValueError("start、end 要带时区")
        calendars = await self._find_calendars()
        per_calendar = await asyncio.gather(
            *(self._events_in(calendar.url, start, end, zone) for calendar in calendars)
        )
        events = [event for events_of_one in per_calendar for event in events_of_one]
        return sorted(events, key=lambda event: _sort_key(event.start, zone))

    async def add_event(
        self, event: CalendarEvent, *, created_at: datetime, calendar_name: str | None
    ) -> AddedEvent:
        calendar = await self._calendar_to_write_in(calendar_name)
        if not re.fullmatch(r"[A-Za-z0-9_.@-]+", event.uid):
            raise ValueError(f"uid 只能用字母、数字和 _ . @ -：{event.uid!r}")
        event_url = f"{calendar.url}{event.uid}.ics"
        await self._send(
            "PUT",
            event_url,
            content=_event_as_ics(event, created_at).encode(),
            headers={
                "Content-Type": "text/calendar; charset=utf-8",
                "If-None-Match": "*",
            },
            accepted={201, 204},
        )
        return AddedEvent(event_url, calendar.name)

    async def delete_event(self, event_url: str) -> bool:
        calendars = await self._find_calendars()
        if not any(event_url.startswith(calendar.url) for calendar in calendars):
            raise ToolFailure("要删的日程不在你的日历里", user_action=None)
        response = await self._send("DELETE", event_url, accepted={200, 204, 404})
        return response.status_code != 404

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _find_calendars(self) -> list[_Calendar]:
        if self._calendars is None:
            home_url = await self._find_calendar_home()
            responses = await self._dav("PROPFIND", home_url, _LIST_CALENDARS_BODY, depth="1")
            found = [
                _Calendar(_as_directory(response.url), _calendar_name(response))
                for response in responses
                if _holds_events(response)
            ]
            if not found:
                raise ToolFailure(
                    "日历账号里没有能放日程的日历",
                    user_action="在日历 App 里确认账号下有日历，且 CalDAV 地址没写错",
                )
            self._calendars = found
        return self._calendars

    async def _calendar_to_write_in(self, calendar_name: str | None) -> _Calendar:
        calendars = await self._find_calendars()
        names = "、".join(calendar.name for calendar in calendars)
        if calendar_name is None:
            if len(calendars) == 1:
                return calendars[0]
            raise ToolFailure(
                f"日历账号里有好几个日历（{names}），不知道加到哪一个",
                user_action="在 argus.yaml 的 calendar.write_calendar 里写上要加到哪个日历的名字",
            )
        for calendar in calendars:
            if calendar.name == calendar_name:
                return calendar
        raise ToolFailure(
            f"日历账号里没有叫「{calendar_name}」的日历（有：{names}）",
            user_action="检查 argus.yaml 的 calendar.write_calendar 是不是和日历 App 里的名字一样",
        )

    async def _find_calendar_home(self) -> str:
        first = await self._dav("PROPFIND", self._base_url, _FIND_HOME_BODY, depth="0")
        for response in first:
            home = _href_inside(response, f"{_CALDAV}calendar-home-set")
            if home is not None:
                return home
        for response in first:
            principal = _href_inside(response, f"{_DAV}current-user-principal")
            if principal is not None:
                second = await self._dav("PROPFIND", principal, _FIND_HOME_BODY, depth="0")
                for principal_response in second:
                    home = _href_inside(principal_response, f"{_CALDAV}calendar-home-set")
                    if home is not None:
                        return home
        raise ToolFailure(
            "日历服务器没说你的日历放在哪",
            user_action="检查 argus.yaml 里的 calendar.url 是不是服务商给的 CalDAV 地址",
        )

    async def _events_in(
        self, calendar_url: str, start: datetime, end: datetime, zone: tzinfo
    ) -> list[CalendarEvent]:
        responses = await self._dav("REPORT", calendar_url, _events_query(start, end), depth="1")
        events: list[CalendarEvent] = []
        for response in responses:
            calendar_data = response.properties.get(f"{_CALDAV}calendar-data")
            if calendar_data is None or not calendar_data.text:
                continue
            events.extend(_expand(calendar_data.text, start, end, zone))
        return events

    async def _dav(self, method: str, url: str, body: str, *, depth: str) -> list[_DavResponse]:
        response = await self._send(
            method,
            url,
            content=body.encode(),
            headers={"Depth": depth, "Content-Type": "application/xml; charset=utf-8"},
            accepted={207},
        )
        return _parse_multistatus(response.content, str(response.url))

    async def _send(
        self,
        method: str,
        url: str,
        *,
        accepted: set[int],
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx2.Response:
        try:
            response = await self._client.request(method, url, content=content, headers=headers)
        except httpx2.TimeoutException as error:
            logger.warning("等日历服务器回复超时：%s", type(error).__name__)
            raise ToolFailure(
                "日历服务器没有及时回复", user_action="过一会儿再试；总是这样的话检查网络"
            ) from error
        except httpx2.RequestError as error:
            logger.warning("连不上日历服务器：%s", type(error).__name__)
            raise ToolFailure(
                "连不上日历服务器", user_action="检查网络，以及 argus.yaml 里的 calendar.url"
            ) from error
        if response.status_code not in accepted:
            logger.warning("日历服务器返回 HTTP %d：%s", response.status_code, method)
            raise _failure_for_status(response.status_code)
        return response


def _events_query(start: datetime, end: datetime) -> str:
    def utc_text(moment: datetime) -> str:
        return moment.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")

    return f"""<c:calendar-query {_NAMESPACES}>
  <d:prop><c:calendar-data/></d:prop>
  <c:filter>
    <c:comp-filter name="VCALENDAR">
      <c:comp-filter name="VEVENT">
        <c:time-range start="{utc_text(start)}" end="{utc_text(end)}"/>
      </c:comp-filter>
    </c:comp-filter>
  </c:filter>
</c:calendar-query>"""


def _event_as_ics(event: CalendarEvent, created_at: datetime) -> str:
    if not isinstance(event.start, datetime) or not isinstance(event.end, datetime):
        raise ValueError("只能写有具体时刻的日程")
    calendar = icalendar.Calendar()
    calendar.add("prodid", "-//ARGUS//calendar//ZH")
    calendar.add("version", "2.0")
    component = icalendar.Event()
    component.add("uid", event.uid)
    component.add("dtstamp", created_at.astimezone(UTC))
    component.add("dtstart", event.start.astimezone(UTC))
    component.add("dtend", event.end.astimezone(UTC))
    component.add("summary", event.title)
    if event.location:
        component.add("location", event.location)
    calendar.add_component(component)
    return calendar.to_ical().decode()


def _as_directory(url: str) -> str:
    return url if url.endswith("/") else url + "/"


def _expand(
    calendar_text: str, start: datetime, end: datetime, zone: tzinfo
) -> list[CalendarEvent]:
    try:
        parsed = icalendar.Calendar.from_ical(calendar_text)
    except ValueError:
        logger.warning("日历里有一份读不懂的日程，跳过")
        return []
    events: list[CalendarEvent] = []
    for component in recurring_ical_events.of(parsed).between(start, end):
        if str(component.get("STATUS", "")).upper() == "CANCELLED":
            continue
        location = str(component.get("LOCATION", "")).strip()
        events.append(
            CalendarEvent(
                uid=str(component.get("UID", "")),
                title=str(component.get("SUMMARY", "")).strip() or "（没写标题）",
                start=_in_zone(component.start, zone),
                end=_in_zone(component.end, zone),
                location=location or None,
            )
        )
    return events


def _in_zone(moment: date | datetime, zone: tzinfo) -> date | datetime:
    if isinstance(moment, datetime):
        return moment.replace(tzinfo=zone) if moment.tzinfo is None else moment.astimezone(zone)
    return moment


def _sort_key(moment: date | datetime, zone: tzinfo) -> datetime:
    if isinstance(moment, datetime):
        return moment
    return datetime.combine(moment, time.min, tzinfo=zone)


def _parse_multistatus(content: bytes, request_url: str) -> list[_DavResponse]:
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError as error:
        raise ToolFailure(
            "日历服务器的回答读不懂",
            user_action="检查 argus.yaml 里的 calendar.url 是不是 CalDAV 地址",
        ) from error
    responses: list[_DavResponse] = []
    for response_element in root.iter(f"{_DAV}response"):
        href = response_element.findtext(f"{_DAV}href")
        if not href:
            continue
        properties: dict[str, ElementTree.Element] = {}
        for propstat in response_element.iter(f"{_DAV}propstat"):
            status = propstat.findtext(f"{_DAV}status") or ""
            if " 200 " not in f" {status} ":
                continue
            for prop in propstat.iter(f"{_DAV}prop"):
                for child in prop:
                    properties[child.tag] = child
        responses.append(_DavResponse(urljoin(request_url, href.strip()), properties))
    return responses


def _href_inside(response: _DavResponse, property_tag: str) -> str | None:
    element = response.properties.get(property_tag)
    href = None if element is None else element.findtext(f"{_DAV}href")
    return urljoin(response.url, href.strip()) if href else None


def _calendar_name(response: _DavResponse) -> str:
    display_name = response.properties.get(f"{_DAV}displayname")
    name = "" if display_name is None else (display_name.text or "").strip()
    return name or "（没有名字的日历）"


def _holds_events(response: _DavResponse) -> bool:
    resource_type = response.properties.get(f"{_DAV}resourcetype")
    if resource_type is None or resource_type.find(f"{_CALDAV}calendar") is None:
        return False
    components = response.properties.get(f"{_CALDAV}supported-calendar-component-set")
    if components is None:
        return True
    return any(component.get("name") == "VEVENT" for component in components)


def _failure_for_status(status_code: int) -> ToolFailure:
    match status_code:
        case 401:
            return ToolFailure(
                "日历服务器不认用户名或授权码",
                user_action="检查 .env 里的 CALDAV_USERNAME、CALDAV_PASSWORD：授权码可能抄错了或被关掉了",
            )
        case 403:
            return ToolFailure(
                "日历服务器不让这个账号用 CalDAV",
                user_action="在服务商的设置里确认已开启 CalDAV，授权码有日历的权限",
            )
        case 404:
            return ToolFailure(
                "日历服务器上找不到这个地址",
                user_action="检查 argus.yaml 里的 calendar.url 是不是服务商给的 CalDAV 地址",
            )
        case _ if status_code >= 500:
            return ToolFailure(
                f"日历服务器出错，HTTP 状态码 {status_code}", user_action="过一会儿再试"
            )
        case _:
            return ToolFailure(
                f"日历服务器不认这个请求，HTTP 状态码 {status_code}",
                user_action="检查 argus.yaml 里的 calendar.url 是不是 CalDAV 地址",
            )
