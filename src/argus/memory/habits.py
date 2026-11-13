from collections.abc import Mapping
from datetime import datetime
from typing import Any

_KNOWN_KEYS = frozenset({"weekdays", "hours"})


def habit_matches(condition: Mapping[str, Any], now: datetime) -> bool:
    if not _KNOWN_KEYS.issuperset(condition):
        return False
    weekdays = condition.get("weekdays")
    if weekdays is not None and now.weekday() not in weekdays:
        return False
    hours = condition.get("hours")
    if hours is not None:
        start, end = hours
        in_range = start <= now.hour < end if start <= end else now.hour >= start or now.hour < end
        if not in_range:
            return False
    return True
