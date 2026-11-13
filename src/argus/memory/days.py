import re
from datetime import date, timedelta

_CHINESE_DIGITS = {
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
_WEEKDAY_INDEX = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
_WEEK_OFFSETS = {"上上": -2, "上": -1, "这": 0, "本": 0, "下": 1, "下下": 2}
_MONTH_OFFSETS = {"上": -1, "这": 0, "本": 0, "下": 1}
_RELATIVE_DAYS = {
    "大前天": -3,
    "前天": -2,
    "昨天": -1,
    "昨儿": -1,
    "今天": 0,
    "今儿": 0,
    "明天": 1,
    "明儿": 1,
    "后天": 2,
    "大后天": 3,
}

_NUMBER = r"(?<![0-9一二三四五六七八九十])(?:[0-9]{1,2}|[一二三四五六七八九十]{1,3})"
_WEEK_PREFIX = r"上上|下下|上|下|这|本"
_WEEK_WORD = r"周|星期|礼拜"

_DAY_PATTERN = re.compile(
    rf"(?P<calendar_day>(?:(?P<year>[0-9]{{4}})年)?"
    rf"(?P<month>{_NUMBER})月(?P<day_of_month>{_NUMBER})[号日])"
    rf"|(?P<relative_month_day>(?P<month_prefix>上|下|这|本)个?月"
    rf"(?P<relative_month_day_number>{_NUMBER})[号日])"
    rf"|(?P<weekend>(?P<weekend_prefix>{_WEEK_PREFIX})?个?周末)"
    rf"|(?P<weekday_phrase>(?P<weekday_prefix>{_WEEK_PREFIX})?个?(?:{_WEEK_WORD})"
    r"(?P<weekday>[一二三四五六日天])(?![次回趟遍]))"
    rf"|(?P<week>(?P<week_prefix>{_WEEK_PREFIX})个?(?:{_WEEK_WORD})"
    r"(?![一二三四五六日天末围边到全岁年期]))"
    r"|(?P<relative>大前天|(?<!以)前天|昨天|昨儿|(?<!如)今天|今儿|明天|明儿|大后天|(?<![以然])后天)"
    rf"|(?P<bare_day>(?P<bare_day_number>{_NUMBER})号(?![线楼门房床位机球码]))"
)


def days_mentioned(text: str, today: date) -> tuple[date, ...]:
    days: set[date] = set()
    for match in _DAY_PATTERN.finditer(text):
        days.update(_days_of(match, today))
    return tuple(sorted(days))


def _days_of(match: re.Match[str], today: date) -> list[date]:
    if match["calendar_day"] is not None:
        return _calendar_days(match["year"], match["month"], match["day_of_month"], today)
    if match["relative_month_day"] is not None:
        return _relative_month_days(
            _MONTH_OFFSETS[match["month_prefix"]], match["relative_month_day_number"], today
        )
    if match["weekend"] is not None:
        saturday = _monday_of_week(today, _week_offset(match["weekend_prefix"])) + timedelta(days=5)
        return [saturday, saturday + timedelta(days=1)]
    if match["weekday_phrase"] is not None:
        return _weekdays(match["weekday_prefix"], _WEEKDAY_INDEX[match["weekday"]], today)
    if match["week"] is not None:
        monday = _monday_of_week(today, _WEEK_OFFSETS[match["week_prefix"]])
        return [monday + timedelta(days=offset) for offset in range(7)]
    if match["relative"] is not None:
        return [today + timedelta(days=_RELATIVE_DAYS[match["relative"]])]
    return _bare_days(match["bare_day_number"], today)


def _weekdays(prefix: str | None, weekday_index: int, today: date) -> list[date]:
    if prefix is not None:
        return [_monday_of_week(today, _WEEK_OFFSETS[prefix]) + timedelta(days=weekday_index)]
    this_week = _monday_of_week(today, 0) + timedelta(days=weekday_index)
    if this_week >= today:
        return [this_week]
    return [this_week, this_week + timedelta(weeks=1)]


def _calendar_days(
    year_text: str | None, month_text: str, day_text: str, today: date
) -> list[date]:
    month, day = _parse_number(month_text), _parse_number(day_text)
    if month is None or day is None:
        return []
    if year_text is not None:
        return _existing_days([(int(year_text), month, day)])
    this_year = _existing_day(today.year, month, day)
    if this_year is not None and this_year >= today:
        return [this_year]
    return _existing_days([(today.year, month, day), (today.year + 1, month, day)])


def _relative_month_days(month_offset: int, day_text: str, today: date) -> list[date]:
    day = _parse_number(day_text)
    if day is None:
        return []
    return _existing_days([(*_shift_month(today, month_offset), day)])


def _bare_days(day_text: str, today: date) -> list[date]:
    day = _parse_number(day_text)
    if day is None:
        return []
    this_month = _existing_day(today.year, today.month, day)
    if this_month is not None and this_month >= today:
        return [this_month]
    return _existing_days([(today.year, today.month, day), (*_shift_month(today, 1), day)])


def _shift_month(today: date, month_offset: int) -> tuple[int, int]:
    months_since_year_zero = today.year * 12 + (today.month - 1) + month_offset
    return months_since_year_zero // 12, months_since_year_zero % 12 + 1


def _monday_of_week(today: date, week_offset: int) -> date:
    return today - timedelta(days=today.weekday()) + timedelta(weeks=week_offset)


def _week_offset(prefix: str | None) -> int:
    return 0 if prefix is None else _WEEK_OFFSETS[prefix]


def _existing_days(candidates: list[tuple[int, int, int]]) -> list[date]:
    return [
        day for day in (_existing_day(*candidate) for candidate in candidates) if day is not None
    ]


def _existing_day(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _parse_number(text: str) -> int | None:
    if text.isascii():
        return int(text)
    tens_text, has_ten, ones_text = text.partition("十")
    if not has_ten:
        return _CHINESE_DIGITS.get(text)
    tens = 1 if not tens_text else _CHINESE_DIGITS.get(tens_text)
    ones = 0 if not ones_text else _CHINESE_DIGITS.get(ones_text)
    if tens is None or ones is None:
        return None
    return tens * 10 + ones
