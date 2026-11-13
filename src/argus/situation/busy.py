from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, time
from enum import Enum
from typing import Protocol


class Busy(Enum):
    FREE = "free"
    IN_MEETING = "in_meeting"
    ASLEEP = "asleep"


@dataclass(frozen=True)
class SleepHours:
    start: time
    end: time

    def __post_init__(self) -> None:
        if self.start == self.end:
            raise ValueError("睡觉的开始和结束不能是同一时刻")

    def covers(self, moment: datetime) -> bool:
        clock_time = moment.time()
        if self.start < self.end:
            return self.start <= clock_time < self.end
        return clock_time >= self.start or clock_time < self.end


class BusySignals(Protocol):
    def in_meeting(self, now: datetime) -> bool: ...


class BusyNow:
    def __init__(self, sleep: SleepHours, meetings: BusySignals | None) -> None:
        self._sleep = sleep
        self._meetings = meetings

    def at(self, now: datetime) -> Busy:
        if self._sleep.covers(now):
            return Busy.ASLEEP
        if self._meetings is not None and self._meetings.in_meeting(now):
            return Busy.IN_MEETING
        return Busy.FREE


BusyAt = Callable[[datetime], Busy]
