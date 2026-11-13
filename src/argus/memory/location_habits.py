import logging
import statistics
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from argus.kernel.clock import Clock
from argus.memory.ports import MemoryStore
from argus.memory.records import NewHabit
from argus.memory.visits import Visit, VisitLog

logger = logging.getLogger(__name__)

LOCATION_SOURCE = "location"

LOOKBACK = timedelta(weeks=4)
KEEP_VISITS = timedelta(days=90)
_REFRESH_EVERY_S = 24 * 3600

_MIN_SHARE_PERCENT = 60


@dataclass(frozen=True)
class _DayKind:
    name: str
    weekdays: tuple[int, ...]
    min_days: int


_GROUPS = (
    _DayKind("工作日", (0, 1, 2, 3, 4), min_days=8),
    _DayKind("周末", (5, 6), min_days=4),
)
_SINGLE_DAYS = tuple(
    _DayKind(name, (weekday,), min_days=3)
    for weekday, name in enumerate(("周一", "周二", "周三", "周四", "周五", "周六", "周日"))
)

_SLOTS = (
    ("凌晨", 0, 5),
    ("早上", 5, 11),
    ("中午", 11, 14),
    ("下午", 14, 18),
    ("晚上", 18, 24),
)


def habits_from_visits(visits: Sequence[Visit]) -> list[NewHabit]:
    days_with_data = {visit.at.date() for visit in visits}
    first_by_day: dict[tuple[str, bool, int], dict[date, datetime]] = defaultdict(dict)
    for visit in visits:
        slot = _slot_of(visit.at.hour)
        firsts = first_by_day[(visit.place, visit.arrived, slot)]
        day = visit.at.date()
        if day not in firsts or visit.at < firsts[day]:
            firsts[day] = visit.at

    habits: list[NewHabit] = []
    for (place, arrived, slot), firsts in sorted(first_by_day.items()):
        found_for_groups = False
        for kind in _GROUPS:
            habit = _habit_if_regular(kind, place, arrived, slot, firsts, days_with_data)
            if habit is not None:
                habits.append(habit)
                found_for_groups = True
        if found_for_groups:
            continue
        for kind in _SINGLE_DAYS:
            habit = _habit_if_regular(kind, place, arrived, slot, firsts, days_with_data)
            if habit is not None:
                habits.append(habit)
    return habits


def _habit_if_regular(
    kind: _DayKind,
    place: str,
    arrived: bool,
    slot: int,
    firsts: dict[date, datetime],
    days_with_data: set[date],
) -> NewHabit | None:
    data_days = [day for day in days_with_data if day.weekday() in kind.weekdays]
    times = [at for day, at in firsts.items() if day.weekday() in kind.weekdays]
    if len(times) < kind.min_days or len(times) * 100 < _MIN_SHARE_PERCENT * len(data_days):
        return None
    slot_name, start_hour, end_hour = _SLOTS[slot]
    verb = "到" if arrived else "离开"
    return NewHabit(
        condition={"weekdays": list(kind.weekdays), "hours": [start_hour, end_hour]},
        statement=f"{kind.name}{slot_name}一般 {_typical_time(times)} 左右{verb}{place}",
        support=len(times),
        last_seen=max(times),
    )


def _slot_of(hour: int) -> int:
    return next(index for index, (_, start, end) in enumerate(_SLOTS) if start <= hour < end)


def _typical_time(times: Sequence[datetime]) -> str:
    minutes = statistics.median(at.hour * 60 + at.minute for at in times)
    rounded = min(round(minutes / 10) * 10, 23 * 60 + 50)
    return f"{rounded // 60}:{rounded % 60:02d}"


class LocationHabitJob:
    def __init__(self, visits: VisitLog, store: MemoryStore, clock: Clock) -> None:
        self._visits = visits
        self._store = store
        self._clock = clock

    async def run(self) -> None:
        while True:
            await self.refresh_once()
            await self._clock.sleep(_REFRESH_EVERY_S)

    async def refresh_once(self) -> None:
        now = self._clock.now()
        try:
            await self._visits.delete_before(now - KEEP_VISITS)
            habits = habits_from_visits(await self._visits.since(now - LOOKBACK))
            await self._store.replace_habits(LOCATION_SOURCE, habits)
        except Exception:
            logger.exception("按位置算规律没算成，明天再算")
            return
        logger.info("按位置算规律：%d 条", len(habits))
