from datetime import timedelta

from pydantic import BaseModel, ConfigDict

from argus.kernel.events import BirthdayToday
from argus.proactive.triggers import Candidate, trigger


class BirthdayParams(BaseModel):
    model_config = ConfigDict(extra="forbid")


@trigger(on=BirthdayToday, id="birthday_today", params=BirthdayParams)
def birthday_today(event: BirthdayToday, params: BirthdayParams) -> Candidate:
    lunar = "" if event.lunar is None else f"（农历{event.lunar}）"
    return Candidate(
        trigger_id="birthday_today",
        text=f"今天是{event.who}的生日{lunar}。",
        value=7,
        urgency=2,
        ttl=timedelta(hours=12),
        dedupe_key=f"{event.who}@{event.day.isoformat()}",
    )
