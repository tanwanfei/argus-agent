import logging
from datetime import timedelta
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from argus.kernel.clock import Clock
from argus.kernel.events import EventBus, TimerDue, TimerMissed
from argus.kernel.spoken_number import parse_chinese_number
from argus.kernel.tools import Permission, ToolContext, ToolFn, ToolResult, tool
from argus.proactive.triggers import Candidate, trigger
from argus.scheduler.scheduler import Scheduler
from argus.scheduler.store import Job

logger = logging.getLogger(__name__)

TIMER_KIND = "proactive.timer"

RING_LATE_FOR = timedelta(minutes=5)

_LONGEST = timedelta(hours=24)


class TimerArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hours: int = Field(0, ge=0, description="过几个小时")
    minutes: int = Field(
        0, ge=0, description="过几分钟；和 hours 加在一起算，比如一个半小时是 hours=1、minutes=30"
    )

    @field_validator("hours", "minutes", mode="before")
    @classmethod
    def _accept_chinese_numbers(cls, value: object) -> object:
        if isinstance(value, str):
            chinese_number = parse_chinese_number(value)
            if chinese_number is not None:
                return chinese_number
        return value

    @model_validator(mode="after")
    def _within_a_day(self) -> Self:
        duration = self.duration
        if duration <= timedelta(0):
            raise ValueError("hours 和 minutes 至少有一个要大于 0")
        if duration > _LONGEST:
            raise ValueError("最长定 24 小时；更晚的提醒要按时刻定，这个工具做不了")
        return self

    @property
    def duration(self) -> timedelta:
        return timedelta(hours=self.hours, minutes=self.minutes)


class CancelTimerArgs(BaseModel):
    job_id: str


def timer_tools(scheduler: Scheduler, clock: Clock) -> tuple[ToolFn, ToolFn]:
    @tool(
        "timer.set",
        Permission.L2,
        params=TimerArgs,
        personal=False,
        description=(
            "定一个闹钟：过多少小时、多少分钟后提醒用户，最长 24 小时。"
            '只按"过多久"定；用户说几点几分的（"明天 8 点提醒我"）用 reminder.set'
        ),
    )
    async def set_timer(args: TimerArgs, ctx: ToolContext) -> ToolResult:
        spoken = _spoken_duration(args.hours, args.minutes)
        due_at = clock.now() + args.duration
        job_id = await scheduler.schedule(
            TIMER_KIND, due_at, {"duration": spoken}, ttl=RING_LATE_FOR
        )
        return ToolResult(
            f"定好了，{spoken}后（{due_at:%H:%M}）提醒用户",
            fields={"duration": spoken},
            undo_args={"job_id": job_id},
        )

    @tool(
        "timer.cancel",
        Permission.L2,
        params=CancelTimerArgs,
        personal=False,
        description="取消刚定的闹钟（撤销用，不给模型）",
    )
    async def cancel_timer(args: CancelTimerArgs, ctx: ToolContext) -> ToolResult:
        await scheduler.cancel(args.job_id)
        return ToolResult("闹钟取消了")

    return set_timer, cancel_timer


class TimerAlarm:
    def __init__(self, bus: EventBus, clock: Clock) -> None:
        self._bus = bus
        self._clock = clock

    async def ring(self, job: Job) -> None:
        self._bus.publish(
            TimerDue(ts=self._clock.now(), job_id=job.id, duration=job.payload["duration"])
        )

    async def missed(self, job: Job) -> None:
        logger.info("闹钟过了时候才开机，没响")
        self._bus.publish(
            TimerMissed(
                ts=self._clock.now(),
                job_id=job.id,
                duration=job.payload["duration"],
                due_at=job.due_at,
            )
        )


class TimerDueParams(BaseModel):
    model_config = ConfigDict(extra="forbid")


@trigger(on=TimerDue, id="timer_due", params=TimerDueParams)
def timer_due(event: TimerDue, params: TimerDueParams) -> Candidate:
    return Candidate(
        trigger_id="timer_due",
        text=f"{event.duration}的闹钟到了。",
        value=10,
        urgency=9,
        ttl=RING_LATE_FOR,
        dedupe_key=f"timer:{event.job_id}",
        personal=False,
        asked_for=True,
    )


@trigger(on=TimerMissed, id="timer_missed", params=TimerDueParams)
def timer_missed(event: TimerMissed, params: TimerDueParams) -> Candidate:
    return Candidate(
        trigger_id="timer_missed",
        text=f"闹钟没响。定的 {event.duration}闹钟 {event.due_at:%H:%M} 到点时，主进程没在运行。",
        value=6,
        urgency=5,
        ttl=timedelta(hours=1),
        dedupe_key=f"timer:{event.job_id}",
        personal=False,
    )


def _spoken_duration(hours: int, minutes: int) -> str:
    parts = []
    if hours:
        parts.append(f"{hours} 小时")
    if minutes:
        parts.append(f"{minutes} 分钟")
    return " ".join(parts)
