import calendar
import re
from dataclasses import dataclass
from datetime import date

from lunar_python import LunarMonth, Solar

_WRITTEN = re.compile(r"(农历|阳历)(\d{4})年(\d{1,2})月(\d{1,2})日?")
_LUNAR = "农历"

_LUNAR_MONTH_NAMES = ("正", "二", "三", "四", "五", "六", "七", "八", "九", "十", "冬", "腊")
_LUNAR_DAY_NAMES = (
    *("初一", "初二", "初三", "初四", "初五", "初六", "初七", "初八", "初九", "初十"),
    *("十一", "十二", "十三", "十四", "十五", "十六", "十七", "十八", "十九", "二十"),
    *("廿一", "廿二", "廿三", "廿四", "廿五", "廿六", "廿七", "廿八", "廿九", "三十"),
)
_A_LEAP_YEAR = 2000


@dataclass(frozen=True)
class Birthday:
    lunar: bool
    month: int
    day: int


def parse_birthday(written: str) -> Birthday | None:
    match = _WRITTEN.fullmatch(written.strip())
    if match is None:
        return None
    kind, _year, month_text, day_text = match.groups()
    month, day = int(month_text), int(day_text)
    lunar = kind == _LUNAR
    if lunar:
        if not (1 <= month <= 12 and 1 <= day <= 30):
            return None
    else:
        try:
            date(_A_LEAP_YEAR, month, day)
        except ValueError:
            return None
    return Birthday(lunar=lunar, month=month, day=day)


def falls_on(birthday: Birthday, day: date) -> bool:
    if not birthday.lunar:
        if (day.month, day.day) == (birthday.month, birthday.day):
            return True
        return (
            (birthday.month, birthday.day) == (2, 29)
            and (day.month, day.day) == (2, 28)
            and not calendar.isleap(day.year)
        )
    lunar_day = Solar.fromYmd(day.year, day.month, day.day).getLunar()
    if lunar_day.getMonth() != birthday.month:
        return False
    if lunar_day.getDay() == birthday.day:
        return True
    days_in_month = LunarMonth.fromYm(lunar_day.getYear(), lunar_day.getMonth()).getDayCount()
    return birthday.day == 30 and days_in_month == 29 and lunar_day.getDay() == 29


def lunar_name(birthday: Birthday) -> str:
    return f"{_LUNAR_MONTH_NAMES[birthday.month - 1]}月{_LUNAR_DAY_NAMES[birthday.day - 1]}"
