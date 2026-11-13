import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import httpx2
from pydantic import BaseModel, ValidationError

from argus.kernel.tools import ToolFailure
from argus.situation.coordinates import Gcj02Point
from argus.situation.geocoding import SENT_DECIMALS, coarsened

logger = logging.getLogger(__name__)

_BASE_URL = "https://restapi.amap.com"
_AROUND_PATH = "/v5/place/around"
_REGEO_PATH = "/v3/geocode/regeo"
_TIMEOUT = httpx2.Timeout(5.0)
_PLACES_WANTED = 10
_FOUND_WANTED = 3
_OK_STATUS = "1"
_MOST_CITY_CODES_KEPT = 256

_KEY_PROBLEM_CODES = frozenset({"10001", "10002", "10005", "10009", "10012", "10013"})
_DAILY_QUOTA_CODES = frozenset({"10003", "10044"})
_PAID_QUOTA_CODES = frozenset({"40000", "40002"})
_TOO_FAST_CODES = frozenset({"10004", "10014", "10015", "10019", "10020", "10021"})
_SERVER_BUSY_CODE = "10016"
_NO_ROUTE_CODES = frozenset({"20800", "20801", "20802", "20803"})
_UNKNOWN_STATUS = "未知"
_SLOW_STATUS = "缓行"
_JAMMED_STATUSES = frozenset({"拥堵", "严重拥堵"})


class TravelMode(StrEnum):
    DRIVING = "driving"
    TRANSIT = "transit"
    WALKING = "walking"
    RIDING = "riding"


_ROUTE_PATHS = {
    TravelMode.DRIVING: "/v5/direction/driving",
    TravelMode.TRANSIT: "/v5/direction/transit/integrated",
    TravelMode.WALKING: "/v5/direction/walking",
    TravelMode.RIDING: "/v5/direction/bicycling",
}


@dataclass(frozen=True)
class NearbyPlace:
    name: str
    kind: str
    distance_m: int | None
    address: str
    rating: float | None
    cost_yuan: float | None
    open_today: str | None


@dataclass(frozen=True)
class FoundPlace:
    name: str
    address: str
    point: Gcj02Point
    city_code: str | None


@dataclass(frozen=True)
class Traffic:
    slow_m: int
    jammed_m: int


@dataclass(frozen=True)
class Route:
    mode: TravelMode
    duration_s: int
    distance_m: int
    taxi_cost_yuan: float | None = None
    tolls_yuan: float | None = None
    lines: tuple[str, ...] = ()
    walking_m: int | None = None
    traffic: Traffic | None = None


class AmapClient:
    def __init__(self, key: str, *, transport: httpx2.AsyncBaseTransport | None = None) -> None:
        self._key = key
        self._client = httpx2.AsyncClient(
            base_url=_BASE_URL,
            trust_env=False,
            timeout=_TIMEOUT,
            transport=transport,
        )
        self._city_codes: dict[Gcj02Point, str | None] = {}

    async def around(self, center: Gcj02Point, *, keyword: str, radius_m: int) -> list[NearbyPlace]:
        reply = await self._ask(
            _AROUND_PATH, self._around_params(center, keyword, radius_m, _PLACES_WANTED), _PoiReply
        )
        return [_nearby_place_of(poi) for poi in reply.pois]

    async def find(self, center: Gcj02Point, *, name: str, radius_m: int) -> FoundPlace | None:
        reply = await self._ask(
            _AROUND_PATH, self._around_params(center, name, radius_m, _FOUND_WANTED), _PoiReply
        )
        for poi in reply.pois:
            point = _point_of(poi.location)
            if point is not None:
                return FoundPlace(
                    name=poi.name,
                    address=_text(poi.address) or "",
                    point=point,
                    city_code=_text(poi.citycode),
                )
        return None

    async def route(
        self,
        origin: Gcj02Point,
        destination: Gcj02Point,
        mode: TravelMode,
        *,
        city_codes: tuple[str, str] | None = None,
    ) -> Route | None:
        params = {
            "origin": _location(origin),
            "destination": _location(destination),
            "show_fields": "cost,tmcs" if mode is TravelMode.DRIVING else "cost",
        }
        if mode is TravelMode.TRANSIT:
            if city_codes is None:
                raise ValueError("算公交要两头的城市编码")
            params |= {"city1": city_codes[0], "city2": city_codes[1], "AlternativeRoute": "1"}
            transit_reply = await self._ask(
                _ROUTE_PATHS[mode], params, _TransitReply, tolerated=_NO_ROUTE_CODES
            )
            if transit_reply.route is None or not transit_reply.route.transits:
                return None
            return _transit_route_of(transit_reply.route.transits[0])
        path_reply = await self._ask(
            _ROUTE_PATHS[mode], params, _PathReply, tolerated=_NO_ROUTE_CODES
        )
        if path_reply.route is None or not path_reply.route.paths:
            return None
        return _path_route_of(mode, path_reply.route)

    async def city_code(self, point: Gcj02Point) -> str | None:
        sent = coarsened(point)
        if sent in self._city_codes:
            return self._city_codes[sent]
        reply = await self._ask(_REGEO_PATH, {"location": _location(sent)}, _RegeoReply)
        code = _text(reply.regeocode.addressComponent.citycode) if reply.regeocode else None
        if len(self._city_codes) >= _MOST_CITY_CODES_KEPT:
            self._city_codes.clear()
        self._city_codes[sent] = code
        return code

    async def aclose(self) -> None:
        await self._client.aclose()

    def _around_params(
        self, center: Gcj02Point, keyword: str, radius_m: int, wanted: int
    ) -> dict[str, str]:
        return {
            "location": _location(center),
            "keywords": keyword,
            "radius": str(radius_m),
            "sortrule": "weight",
            "show_fields": "business",
            "page_size": str(wanted),
        }

    async def _ask[ReplyT: _Reply](
        self,
        path: str,
        params: dict[str, str],
        reply_type: type[ReplyT],
        *,
        tolerated: frozenset[str] = frozenset(),
    ) -> ReplyT:
        try:
            response = await self._client.get(path, params={"key": self._key, **params})
        except httpx2.TimeoutException as error:
            logger.warning("等高德地图回复超时：%s", type(error).__name__)
            raise ToolFailure(
                "地图服务没有及时回复", user_action="过一会儿再试；总是这样的话检查网络"
            ) from None
        except httpx2.RequestError as error:
            logger.warning("连不上高德地图：%s", type(error).__name__)
            raise ToolFailure("连不上地图服务", user_action="检查主机的网络") from None

        if not response.is_success:
            logger.warning("高德地图返回 HTTP %d", response.status_code)
            raise ToolFailure(
                f"地图服务出错，HTTP 状态码 {response.status_code}", user_action="过一会儿再试"
            )
        try:
            reply = reply_type.model_validate_json(response.content)
        except ValidationError:
            raise _unreadable() from None
        if reply.status != _OK_STATUS and reply.infocode not in tolerated:
            logger.warning("高德地图返回 infocode %s", reply.infocode)
            raise _failure_for(reply.infocode)
        return reply


def _location(point: Gcj02Point) -> str:
    sent = coarsened(point)
    return f"{sent.lng:.{SENT_DECIMALS}f},{sent.lat:.{SENT_DECIMALS}f}"


def _failure_for(infocode: str) -> ToolFailure:
    if infocode in _KEY_PROBLEM_CODES:
        return ToolFailure(
            f"地图服务不认这个 key（infocode {infocode}）",
            user_action=(
                "检查 .env 里的 AMAP_KEY，和高德控制台里这个 key：要「Web 服务」类型，没设 IP 白名单"
            ),
        )
    if infocode in _DAILY_QUOTA_CODES:
        return ToolFailure(
            "地图服务今天的额度用完了", user_action="明天再试；去高德控制台看一下额度"
        )
    if infocode in _PAID_QUOTA_CODES:
        return ToolFailure(
            "高德的免费额度或买的流量包用完了",
            user_action="到高德控制台买流量包（搜索、路线规划各买各的）",
        )
    if infocode in _TOO_FAST_CODES:
        return ToolFailure("查得太频繁，地图服务限流了", user_action="过一会儿再试")
    if infocode == _SERVER_BUSY_CODE or infocode.startswith("3"):
        return ToolFailure(f"地图服务出错（infocode {infocode}）", user_action="过一会儿再试")
    return ToolFailure(
        f"地图服务不认这个请求（infocode {infocode}）",
        user_action="对着高德的文档查一下接口是不是改了",
    )


def _unreadable() -> ToolFailure:
    return ToolFailure("地图服务的回复看不懂", user_action="可能是高德改了接口，对着官方文档查一下")


def _nearby_place_of(poi: "_Poi") -> NearbyPlace:
    business = poi.business or _Business()
    return NearbyPlace(
        name=poi.name,
        kind=(_text(poi.type) or "").split(";")[-1],
        distance_m=_whole_number(poi.distance),
        address=_text(poi.address) or "",
        rating=_number(business.rating),
        cost_yuan=_number(business.cost),
        open_today=_text(business.opentime_today),
    )


def _point_of(location: str | list[Any] | None) -> Gcj02Point | None:
    parts = (_text(location) or "").split(",")
    if len(parts) != 2:
        return None
    try:
        return Gcj02Point(lat=float(parts[1]), lng=float(parts[0]))
    except ValueError:
        return None


def _path_route_of(mode: TravelMode, route: "_PathRoute") -> Route:
    path = route.paths[0]
    cost = path.cost or _PathCost()
    duration_s = _whole_number(cost.duration)
    if duration_s is None:
        duration_s = _whole_number(path.duration)
    distance_m = _whole_number(path.distance)
    if duration_s is None or distance_m is None:
        raise _unreadable()
    driving = mode is TravelMode.DRIVING
    return Route(
        mode=mode,
        duration_s=duration_s,
        distance_m=distance_m,
        taxi_cost_yuan=_number(route.taxi_cost) if driving else None,
        tolls_yuan=_number(cost.tolls) if driving else None,
        traffic=_traffic_of(path) if driving else None,
    )


def _traffic_of(path: "_Path") -> Traffic | None:
    slow_m = jammed_m = known_m = 0
    for step in path.steps:
        for tmc in step.tmcs:
            length_m = _whole_number(tmc.tmc_distance) or 0
            status = _text(tmc.tmc_status)
            if status is None or status == _UNKNOWN_STATUS:
                continue
            known_m += length_m
            if status == _SLOW_STATUS:
                slow_m += length_m
            elif status in _JAMMED_STATUSES:
                jammed_m += length_m
    if known_m == 0:
        return None
    return Traffic(slow_m=slow_m, jammed_m=jammed_m)


def _transit_route_of(transit: "_Transit") -> Route:
    duration_s = _whole_number(transit.cost.duration if transit.cost else None)
    distance_m = _whole_number(transit.distance)
    if duration_s is None or distance_m is None:
        raise _unreadable()
    lines = []
    for segment in transit.segments:
        if segment.bus is not None and segment.bus.buslines:
            lines.append(_short_line_name(segment.bus.buslines[0].name))
        elif segment.railway is not None and _text(segment.railway.name):
            lines.append(_short_line_name(segment.railway.name))
    return Route(
        mode=TravelMode.TRANSIT,
        duration_s=duration_s,
        distance_m=distance_m,
        lines=tuple(lines),
        walking_m=_whole_number(transit.walking_distance),
    )


def _short_line_name(name: str | list[Any] | None) -> str:
    return (_text(name) or "").split("(")[0].strip()


def _text(value: str | list[Any] | None) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _number(value: str | list[Any] | None) -> float | None:
    text = _text(value)
    if text is None:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _whole_number(value: str | list[Any] | None) -> int | None:
    number = _number(value)
    return round(number) if number is not None else None


type _Field = str | list[Any] | None


class _Reply(BaseModel):
    status: str
    infocode: str = ""


class _Business(BaseModel):
    rating: _Field = None
    cost: _Field = None
    opentime_today: _Field = None


class _Poi(BaseModel):
    name: str
    type: _Field = None
    distance: _Field = None
    address: _Field = None
    location: _Field = None
    citycode: _Field = None
    business: _Business | None = None


class _PoiReply(_Reply):
    pois: list[_Poi] = []


class _PathCost(BaseModel):
    duration: _Field = None
    tolls: _Field = None


class _Tmc(BaseModel):
    tmc_status: _Field = None
    tmc_distance: _Field = None


class _Step(BaseModel):
    tmcs: list[_Tmc] = []


class _Path(BaseModel):
    distance: _Field = None
    duration: _Field = None
    cost: _PathCost | None = None
    steps: list[_Step] = []


class _PathRoute(BaseModel):
    taxi_cost: _Field = None
    paths: list[_Path] = []


class _PathReply(_Reply):
    route: _PathRoute | None = None


class _Line(BaseModel):
    name: _Field = None


class _Bus(BaseModel):
    buslines: list[_Line] = []


class _Segment(BaseModel):
    bus: _Bus | None = None
    railway: _Line | None = None


class _TransitCost(BaseModel):
    duration: _Field = None


class _Transit(BaseModel):
    distance: _Field = None
    walking_distance: _Field = None
    cost: _TransitCost | None = None
    segments: list[_Segment] = []


class _TransitRoute(BaseModel):
    transits: list[_Transit] = []


class _TransitReply(_Reply):
    route: _TransitRoute | None = None


class _AddressComponent(BaseModel):
    citycode: _Field = None


class _Regeocode(BaseModel):
    addressComponent: _AddressComponent


class _RegeoReply(_Reply):
    regeocode: _Regeocode | None = None
