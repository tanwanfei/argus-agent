import logging
from datetime import datetime, time, timedelta
from pathlib import Path

from argus.infra.knowledge.birthday_dates import falls_on, lunar_name, parse_birthday
from argus.kernel.clock import Clock
from argus.kernel.events import BirthdayToday, EventBus
from argus.memory.profile import parse_profile

logger = logging.getLogger(__name__)

_CHECK_AT = time(6, 50)
_SELF = "我"
_BIRTHDAY = "生日"


class BirthdayWatch:
    def __init__(self, profile_path: Path, clock: Clock, bus: EventBus) -> None:
        self._profile_path = profile_path
        self._clock = clock
        self._bus = bus

    async def run(self) -> None:
        while True:
            self.check_once()
            now = self._clock.now()
            await self._clock.sleep((_next_check_at(now) - now).total_seconds())

    def check_once(self) -> None:
        today = self._clock.now().date()
        try:
            text = self._profile_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            logger.info("没有档案（关于我.md），不查生日")
            return
        except (OSError, UnicodeDecodeError) as error:
            logger.warning("档案读不了，这次不查生日：%s", type(error).__name__)
            return
        unreadable = 0
        for entry in parse_profile(text).entries:
            if entry.attribute != _BIRTHDAY or entry.subject == _SELF:
                continue
            birthday = parse_birthday(entry.value)
            if birthday is None:
                unreadable += 1
                continue
            if falls_on(birthday, today):
                self._bus.publish(
                    BirthdayToday(
                        who=entry.subject,
                        day=today,
                        lunar=lunar_name(birthday) if birthday.lunar else None,
                        ts=self._clock.now(),
                    )
                )
        if unreadable:
            logger.warning(
                "档案里有 %d 条生日看不懂写法，没算（要写成农历或阳历加年月日）", unreadable
            )


def _next_check_at(after: datetime) -> datetime:
    today_at = datetime.combine(after.date(), _CHECK_AT, tzinfo=after.tzinfo)
    return today_at if today_at > after else today_at + timedelta(days=1)
