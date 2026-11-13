from datetime import datetime, timedelta
from typing import Protocol

from argus.infra.integrations.calendar.client import CalendarEvent
from argus.kernel.clock import Clock
from argus.kernel.events import CalendarUpcoming, EventBus
from argus.kernel.tools import ToolFailure

_TICK_SECONDS = 60
_ASK_SERVER_EVERY = timedelta(minutes=15)
_LOOK_AHEAD = timedelta(hours=1)
_ASK_SPAN = _LOOK_AHEAD + _ASK_SERVER_EVERY


class UpcomingEvents(Protocol):
    async def events_between(self, start: datetime, end: datetime) -> list[CalendarEvent]: ...


class CalendarWatch:
    def __init__(self, calendar: UpcomingEvents, clock: Clock, bus: EventBus) -> None:
        self._calendar = calendar
        self._clock = clock
        self._bus = bus
        self._known: list[CalendarEvent] = []
        self._asked_at: datetime | None = None

    async def run(self) -> None:
        while True:
            await self.check_once()
            await self._clock.sleep(_TICK_SECONDS)

    async def check_once(self) -> None:
        now = self._clock.now()
        if self._asked_at is None or now - self._asked_at >= _ASK_SERVER_EVERY:
            await self._ask_server(now)
        for event in self._known:
            if not isinstance(event.start, datetime) or not isinstance(event.end, datetime):
                continue
            if now < event.start <= now + _LOOK_AHEAD:
                self._bus.publish(
                    CalendarUpcoming(
                        uid=event.uid,
                        title=event.title,
                        start=event.start,
                        end=event.end,
                        location=event.location,
                        ts=now,
                    )
                )

    def in_meeting(self, now: datetime) -> bool:
        return any(
            isinstance(event.start, datetime)
            and isinstance(event.end, datetime)
            and event.start <= now < event.end
            for event in self._known
        )

    async def _ask_server(self, now: datetime) -> None:
        self._asked_at = now
        try:
            self._known = await self._calendar.events_between(now, now + _ASK_SPAN)
        except ToolFailure:
            return
