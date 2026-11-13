from dataclasses import dataclass
from datetime import datetime, time, timedelta
from enum import Enum
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from argus.kernel.clock import Clock
from argus.kernel.events import EventBus, GeofenceEntered, GeofenceExited, PlaceReminderDue
from argus.kernel.ids import IdGen
from argus.kernel.tools import Permission, ToolContext, ToolFailure, ToolFn, ToolResult, tool
from argus.proactive.triggers import Candidate, trigger
from argus.situation.places import PlaceStore

_SAY_WITHIN = timedelta(minutes=5)


class Crossing(Enum):
    ARRIVE = "arrive"
    LEAVE = "leave"


@dataclass(frozen=True)
class PlaceReminder:
    id: str
    place: str
    crossing: Crossing
    what: str
    every_time: bool
    created_at: datetime
    workdays_only: bool = False
    window_start: time | None = None
    window_end: time | None = None

    def matches(self, place: str, crossing: Crossing, at: datetime) -> bool:
        if place != self.place or crossing is not self.crossing:
            return False
        if self.workdays_only and at.weekday() >= 5:
            return False
        clock_time = at.time()
        if self.window_start is not None and clock_time < self.window_start:
            return False
        return self.window_end is None or clock_time < self.window_end


class PlaceReminderStore(Protocol):
    async def add(self, reminder: PlaceReminder) -> None: ...

    async def all(self) -> list[PlaceReminder]: ...

    async def remove(self, reminder_id: str) -> bool: ...


class SetPlaceReminderArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    place: str = Field(description='地点的名字，要是用户录过的，如"公司""家"')
    crossing: Literal["arrive", "leave"] = Field(
        description="arrive 是到了那里时提醒，leave 是离开那里时提醒"
    )
    what: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=50)] = (
        Field(description='要提醒用户做的事，用做事的说法，如"打卡""交报销单"；响时念"提醒你打卡"')
    )
    every_time: bool = Field(
        default=False,
        description='用户说"每次""每天"时填 true，每次进出都提醒；只提醒一次（"下次到公司"）填 false',
    )
    workdays_only: bool = Field(
        default=False, description='用户说"工作日""上班的日子"时填 true：只在周一到周五提醒'
    )
    after: time | None = Field(
        default=None,
        description='用户说了"下午 5 点以后"这类时才填，本地钟点，格式 17:00：这之前进出不提醒',
    )
    before: time | None = Field(
        default=None,
        description='用户说了"上午 10 点以前"这类时才填，本地钟点，格式 10:00：这之后进出不提醒',
    )

    @model_validator(mode="after")
    def _window_does_not_cross_midnight(self) -> "SetPlaceReminderArgs":
        if self.after is not None and self.before is not None and self.after >= self.before:
            raise ValueError("after 要早于 before：时段不跨过半夜")
        return self


class CancelPlaceReminderArgs(BaseModel):
    reminder_id: str


class RemovePlaceReminderArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reminder_id: str = Field(description="要取消的地点提醒，reminder.list 列出来的 reminder_id")


class RestorePlaceReminderArgs(BaseModel):
    reminder_id: str
    place: str
    crossing: Crossing
    what: str
    every_time: bool
    created_at: datetime
    workdays_only: bool
    window_start: time | None
    window_end: time | None


def place_reminder_tools(
    store: PlaceReminderStore, places: PlaceStore, clock: Clock, ids: IdGen
) -> tuple[ToolFn, ToolFn]:
    @tool(
        "place_reminder.set",
        Permission.L2,
        params=SetPlaceReminderArgs,
        sensitive=("place", "what"),
        description=(
            "到了或离开用户录过的某个地点时提醒他做一件事，可以每次都提醒，可以只在工作日、某个钟点以后。"
            '用户说"下次到公司提醒我交报销单""工作日下午 5 点以后离开公司提醒我打卡"时用。'
            "按几点提醒的用 reminder.set"
        ),
    )
    async def set_reminder(args: SetPlaceReminderArgs, ctx: ToolContext) -> ToolResult:
        if args.place not in {place.name for place in await places.all()}:
            raise ToolFailure(
                f"没录过叫「{args.place}」的地点，定不了",
                user_action=f"到了那里说「把这里记成{args.place}」，再定这个提醒",
            )
        reminder = PlaceReminder(
            id=ids.new("place_reminder"),
            place=args.place,
            crossing=Crossing(args.crossing),
            what=args.what,
            every_time=args.every_time,
            created_at=clock.now(),
            workdays_only=args.workdays_only,
            window_start=args.after,
            window_end=args.before,
        )
        await store.add(reminder)
        return ToolResult(
            f"定好了，{describe_place_reminder(reminder)}", undo_args={"reminder_id": reminder.id}
        )

    @tool(
        "place_reminder.cancel",
        Permission.L2,
        params=CancelPlaceReminderArgs,
        description="取消刚定的地点提醒（撤销用，不给模型）",
    )
    async def cancel_reminder(args: CancelPlaceReminderArgs, ctx: ToolContext) -> ToolResult:
        await store.remove(args.reminder_id)
        return ToolResult("地点提醒取消了")

    return set_reminder, cancel_reminder


def manage_place_reminder_tools(store: PlaceReminderStore) -> tuple[ToolFn, ToolFn]:
    @tool(
        "place_reminder.remove",
        Permission.L2,
        params=RemovePlaceReminderArgs,
        description="取消用户定着的一个按地点的提醒，每次都响的以后也不再提醒。reminder_id 先用 reminder.list 查",
    )
    async def remove_reminder(args: RemovePlaceReminderArgs, ctx: ToolContext) -> ToolResult:
        reminder = next((each for each in await store.all() if each.id == args.reminder_id), None)
        if reminder is None or not await store.remove(args.reminder_id):
            raise ToolFailure(
                "没有这个地点提醒：可能已经响过、取消了，或者 reminder_id 不对", user_action=None
            )
        return ToolResult(
            f"取消了：{describe_place_reminder(reminder)}",
            undo_args={
                "reminder_id": reminder.id,
                "place": reminder.place,
                "crossing": reminder.crossing.value,
                "what": reminder.what,
                "every_time": reminder.every_time,
                "created_at": reminder.created_at.isoformat(),
                "workdays_only": reminder.workdays_only,
                "window_start": _clock_time_or_none(reminder.window_start),
                "window_end": _clock_time_or_none(reminder.window_end),
            },
        )

    @tool(
        "place_reminder.restore",
        Permission.L2,
        params=RestorePlaceReminderArgs,
        sensitive=("place", "what"),
        description="撤销取消的地点提醒：照原样放回去（撤销用，不给模型）",
    )
    async def restore_reminder(args: RestorePlaceReminderArgs, ctx: ToolContext) -> ToolResult:
        await store.add(
            PlaceReminder(
                id=args.reminder_id,
                place=args.place,
                crossing=args.crossing,
                what=args.what,
                every_time=args.every_time,
                created_at=args.created_at,
                workdays_only=args.workdays_only,
                window_start=args.window_start,
                window_end=args.window_end,
            )
        )
        return ToolResult("地点提醒恢复了")

    return remove_reminder, restore_reminder


def _clock_time_or_none(clock_time: time | None) -> str | None:
    return None if clock_time is None else clock_time.isoformat(timespec="minutes")


def describe_place_reminder(reminder: PlaceReminder) -> str:
    when = "每次" if reminder.every_time else "下次"
    moved = "到" if reminder.crossing is Crossing.ARRIVE else "离开"
    limits = []
    if reminder.workdays_only:
        limits.append("只在工作日")
    if reminder.window_start is not None:
        limits.append(f"{reminder.window_start:%H:%M} 以后")
    if reminder.window_end is not None:
        limits.append(f"{reminder.window_end:%H:%M} 以前")
    limited = f"（{'、'.join(limits)}）" if limits else ""
    return f"{when}{moved}{reminder.place}时{limited}提醒用户{reminder.what}"


class PlaceReminderBell:
    def __init__(self, store: PlaceReminderStore, bus: EventBus) -> None:
        self._store = store
        self._bus = bus

    async def on_entered(self, event: GeofenceEntered) -> None:
        await self._ring(event.place, Crossing.ARRIVE, event.ts)

    async def on_exited(self, event: GeofenceExited) -> None:
        await self._ring(event.place, Crossing.LEAVE, event.ts)

    async def _ring(self, place: str, crossing: Crossing, at: datetime) -> None:
        for reminder in await self._store.all():
            if not reminder.matches(place, crossing, at):
                continue
            if not reminder.every_time and not await self._store.remove(reminder.id):
                continue
            self._bus.publish(
                PlaceReminderDue(
                    ts=at,
                    reminder_id=reminder.id,
                    place=place,
                    arrived=crossing is Crossing.ARRIVE,
                    what=reminder.what,
                )
            )


class PlaceReminderParams(BaseModel):
    model_config = ConfigDict(extra="forbid")


@trigger(on=PlaceReminderDue, id="place_reminder_due", params=PlaceReminderParams)
def place_reminder_due(event: PlaceReminderDue, params: PlaceReminderParams) -> Candidate:
    moved = "到" if event.arrived else "离开"
    return Candidate(
        trigger_id="place_reminder_due",
        text=f"{moved}{event.place}了，提醒你{event.what}。",
        value=10,
        urgency=9,
        ttl=_SAY_WITHIN,
        dedupe_key=f"place_reminder:{event.reminder_id}:{event.ts.isoformat()}",
        asked_for=True,
    )
