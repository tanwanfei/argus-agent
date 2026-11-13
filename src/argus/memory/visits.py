from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from argus.kernel.events import GeofenceEntered, GeofenceExited


@dataclass(frozen=True)
class Visit:
    place: str
    arrived: bool
    at: datetime


class VisitLog(Protocol):
    async def add(self, visit: Visit) -> None: ...

    async def since(self, start: datetime) -> list[Visit]: ...

    async def delete_before(self, cutoff: datetime) -> int: ...


class VisitRecorder:
    def __init__(self, log: VisitLog) -> None:
        self._log = log

    async def on_entered(self, event: GeofenceEntered) -> None:
        await self._log.add(Visit(place=event.place, arrived=True, at=event.ts))

    async def on_exited(self, event: GeofenceExited) -> None:
        await self._log.add(Visit(place=event.place, arrived=False, at=event.ts))
