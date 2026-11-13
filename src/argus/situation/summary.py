from datetime import datetime

_WEEKDAY_NAMES = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

MUTED_NOTE = "今天开着不打扰，主动提醒都攒进简报"


def situation_summary(now: datetime, *, location: str | None, muted: bool = False) -> str:
    weekday = _WEEKDAY_NAMES[now.weekday()]
    parts = [f"{now.year}年{now.month}月{now.day}日 {weekday} {now:%H:%M}"]
    if location is not None:
        parts.append(location)
    if muted:
        parts.append(MUTED_NOTE)
    return "，".join(parts)
