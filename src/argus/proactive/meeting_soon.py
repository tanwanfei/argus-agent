import math
from datetime import timedelta

from pydantic import BaseModel, ConfigDict, Field

from argus.kernel.events import CalendarUpcoming
from argus.proactive.triggers import Candidate, trigger


class MeetingSoonParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    minutes_before: int = Field(default=10, ge=1, le=60)


@trigger(on=CalendarUpcoming, id="meeting_soon", params=MeetingSoonParams)
def meeting_soon(event: CalendarUpcoming, params: MeetingSoonParams) -> Candidate | None:
    left = event.start - event.ts
    if left > timedelta(minutes=params.minutes_before):
        return None
    minutes_left = math.ceil(left.total_seconds() / 60)
    text = (
        f"{event.title} {event.start.hour}:{event.start.minute:02d} 开始，还有 {minutes_left} 分钟"
    )
    if event.location:
        text += f"，在{event.location}"
    return Candidate(
        trigger_id="meeting_soon",
        text=f"{text}。",
        value=8,
        urgency=9,
        ttl=left,
        dedupe_key=event.dedupe_key,
    )
