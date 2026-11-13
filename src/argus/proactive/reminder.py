import logging
from datetime import datetime, timedelta
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from argus.kernel.clock import Clock
from argus.kernel.events import EventBus, ReminderDue, ReminderMissed
from argus.kernel.ids import IdGen
from argus.kernel.tools import Permission, ToolContext, ToolFailure, ToolFn, ToolResult, tool
from argus.proactive.place_reminder import PlaceReminderStore, describe_place_reminder
from argus.proactive.triggers import Candidate, trigger
from argus.scheduler.scheduler import Scheduler
from argus.scheduler.store import Job

logger = logging.getLogger(__name__)

REMINDER_KIND = "proactive.reminder"

RING_LATE_FOR = timedelta(hours=2)

_FURTHEST = timedelta(days=366)

_SAY_WITHIN = timedelta(minutes=5)

_WEEKDAYS = "一二三四五六日"


class ReminderArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    what: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=50)] = (
        Field(
            description='要提醒用户做的事，用做事的说法，如"吃药""带伞""给妈妈打电话"；到点念"提醒你吃药"'
        )
    )
    at: datetime = Field(
        description="几点提醒，用户所在地的本地时间，格式 2026-10-30T08:00；明天、下周三要按情境里的日期算出来；"
        "重复的提醒填第一次"
    )
    every_days: int | None = Field(
        default=None,
        ge=1,
        le=30,
        description="隔几天重复一次：每天是 1，隔一天（隔天）一次是 2，每周是 7；只提醒一次就不填",
    )


class CancelReminderArgs(BaseModel):
    reminder_id: str


class ListRemindersArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RemoveReminderArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reminder_id: str = Field(description="要取消的提醒，reminder.list 列出来的 reminder_id")


class RestoreReminderArgs(BaseModel):
    reminder_id: str
    what: str
    due_at: datetime
    every_days: int | None


def reminder_tools(scheduler: Scheduler, clock: Clock, ids: IdGen) -> tuple[ToolFn, ToolFn]:
    @tool(
        "reminder.set",
        Permission.L2,
        params=ReminderArgs,
        sensitive=("what",),
        description=(
            "到某个时刻提醒用户做一件事（吃药、带伞、交周报），可以隔几天重复，到点念「提醒你……」。"
            '用户说"明天早上 8 点提醒我带伞""从明天起每隔一天 8 点提醒我吃药"时用。'
            '"过多久"的（"10 分钟后叫我"）用 timer.set；会议、约会这类日程用 calendar.add；'
            "到了、离开某个地点时提醒的用 place_reminder.set"
        ),
    )
    async def set_reminder(args: ReminderArgs, ctx: ToolContext) -> ToolResult:
        now = clock.now()
        zone = now.tzinfo
        assert zone is not None
        due_at = args.at.astimezone(zone) if args.at.tzinfo else args.at.replace(tzinfo=zone)
        if due_at <= now:
            raise ToolFailure("这个时刻已经过了", user_action=None)
        if due_at - now > _FURTHEST:
            raise ToolFailure("最远只能定到一年以后", user_action=None)
        reminder_id = ids.new("reminder")
        payload: dict[str, object] = {"what": args.what}
        if args.every_days is not None:
            payload["every_days"] = args.every_days
        await scheduler.schedule(REMINDER_KIND, due_at, payload, key=reminder_id, ttl=RING_LATE_FOR)
        if args.every_days is None:
            text = f"定好了，{_describe_moment(due_at)}提醒用户{args.what}"
        else:
            repeat = _describe_repeat(args.every_days)
            text = f"定好了，从 {_describe_moment(due_at)}起{repeat}提醒用户{args.what}"
        return ToolResult(text, undo_args={"reminder_id": reminder_id})

    @tool(
        "reminder.cancel",
        Permission.L2,
        params=CancelReminderArgs,
        description="取消刚定的提醒（撤销用，不给模型）",
    )
    async def cancel_reminder(args: CancelReminderArgs, ctx: ToolContext) -> ToolResult:
        await scheduler.cancel(args.reminder_id)
        return ToolResult("提醒取消了")

    return set_reminder, cancel_reminder


def manage_reminder_tools(
    scheduler: Scheduler, place_reminders: PlaceReminderStore
) -> tuple[ToolFn, ToolFn, ToolFn]:
    @tool(
        "reminder.list",
        Permission.L1,
        params=ListRemindersArgs,
        description=(
            "列出用户定着的提醒：按时刻的（reminder.set 定的）提醒什么、多久一次、下一次几点，"
            "按地点的（place_reminder.set 定的）到了还是离开哪里时提醒什么。"
            '用户问"我定了哪些提醒"时用；要取消某个提醒，先用它查到 reminder_id。闹钟（timer.set）不在里面'
        ),
    )
    async def list_reminders(args: ListRemindersArgs, ctx: ToolContext) -> ToolResult:
        jobs = await scheduler.pending(REMINDER_KIND)
        at_places = await place_reminders.all()
        if not jobs and not at_places:
            return ToolResult("没有定着的提醒")
        lines = []
        if jobs:
            lines.append(f"按时刻的提醒 {len(jobs)} 个（取消用 reminder.remove）：")
            lines += [f"- {_describe_job(job)}（reminder_id：{_reminder_id(job)}）" for job in jobs]
        if at_places:
            lines.append(f"按地点的提醒 {len(at_places)} 个（取消用 place_reminder.remove）：")
            lines += [
                f"- {describe_place_reminder(reminder)}（reminder_id：{reminder.id}）"
                for reminder in at_places
            ]
        return ToolResult("\n".join(lines))

    @tool(
        "reminder.remove",
        Permission.L2,
        params=RemoveReminderArgs,
        description=(
            "取消用户定着的一个按时刻的提醒，重复的以后都不再提醒。reminder_id 先用 reminder.list 查；"
            "按地点的提醒用 place_reminder.remove"
        ),
    )
    async def remove_reminder(args: RemoveReminderArgs, ctx: ToolContext) -> ToolResult:
        pending = await scheduler.pending(REMINDER_KIND)
        job = next((job for job in pending if _reminder_id(job) == args.reminder_id), None)
        if job is None:
            raise ToolFailure(
                "没有这个提醒：可能已经念过、取消了，或者 reminder_id 不对", user_action=None
            )
        await scheduler.cancel(args.reminder_id)
        return ToolResult(
            f"取消了：{_describe_job(job)}",
            undo_args={
                "reminder_id": args.reminder_id,
                "what": job.payload["what"],
                "due_at": job.due_at.isoformat(),
                "every_days": job.payload.get("every_days"),
            },
        )

    @tool(
        "reminder.restore",
        Permission.L2,
        params=RestoreReminderArgs,
        sensitive=("what",),
        description="撤销取消的提醒：照原样排回去（撤销用，不给模型）",
    )
    async def restore_reminder(args: RestoreReminderArgs, ctx: ToolContext) -> ToolResult:
        payload: dict[str, object] = {"what": args.what}
        if args.every_days is not None:
            payload["every_days"] = args.every_days
        await scheduler.schedule(
            REMINDER_KIND, args.due_at, payload, key=args.reminder_id, ttl=RING_LATE_FOR
        )
        return ToolResult("提醒恢复了")

    return list_reminders, remove_reminder, restore_reminder


class ReminderAlarm:
    def __init__(self, bus: EventBus, clock: Clock, scheduler: Scheduler) -> None:
        self._bus = bus
        self._clock = clock
        self._scheduler = scheduler

    async def ring(self, job: Job) -> None:
        await self._schedule_next(job)
        self._bus.publish(
            ReminderDue(ts=self._clock.now(), job_id=job.id, what=job.payload["what"])
        )

    async def missed(self, job: Job) -> None:
        logger.info("提醒过了时候才开机，没念")
        await self._schedule_next(job)
        self._bus.publish(
            ReminderMissed(
                ts=self._clock.now(), job_id=job.id, what=job.payload["what"], due_at=job.due_at
            )
        )

    async def _schedule_next(self, job: Job) -> None:
        every_days = job.payload.get("every_days")
        if every_days is None:
            return
        period = timedelta(days=every_days)
        periods_passed = max(0, (self._clock.now() - job.due_at) // period)
        next_due = job.due_at + (periods_passed + 1) * period
        await self._scheduler.schedule(
            REMINDER_KIND, next_due, job.payload, key=job.key, ttl=RING_LATE_FOR
        )


class ReminderParams(BaseModel):
    model_config = ConfigDict(extra="forbid")


@trigger(on=ReminderDue, id="reminder_due", params=ReminderParams)
def reminder_due(event: ReminderDue, params: ReminderParams) -> Candidate:
    return Candidate(
        trigger_id="reminder_due",
        text=f"提醒你{event.what}。",
        value=10,
        urgency=9,
        ttl=_SAY_WITHIN,
        dedupe_key=f"reminder:{event.job_id}",
        asked_for=True,
    )


@trigger(on=ReminderMissed, id="reminder_missed", params=ReminderParams)
def reminder_missed(event: ReminderMissed, params: ReminderParams) -> Candidate:
    return Candidate(
        trigger_id="reminder_missed",
        text=f"提醒没响：{_describe_moment(event.due_at)}要提醒你{event.what}，那时主进程没在运行。",
        value=6,
        urgency=5,
        ttl=timedelta(hours=1),
        dedupe_key=f"reminder:{event.job_id}",
    )


def _describe_moment(moment: datetime) -> str:
    weekday = _WEEKDAYS[moment.weekday()]
    return f"{moment.month} 月 {moment.day} 日（周{weekday}）{moment.hour}:{moment.minute:02d}"


def _reminder_id(job: Job) -> str:
    return job.key or job.id


def _describe_job(job: Job) -> str:
    what = job.payload["what"]
    every_days = job.payload.get("every_days")
    if every_days is None:
        return f"{what}，{_describe_moment(job.due_at)}"
    return f"{what}，{_describe_repeat(every_days)}一次，下一次 {_describe_moment(job.due_at)}"


def _describe_repeat(every_days: int) -> str:
    return {1: "每天", 2: "隔一天", 7: "每周"}.get(every_days, f"每 {every_days} 天")
