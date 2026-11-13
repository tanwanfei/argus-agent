from datetime import date
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from argus.delivery.outbound import Verdict
from argus.kernel.clock import Clock
from argus.kernel.tools import Permission, ToolContext, ToolFn, ToolResult, tool
from argus.proactive.decision import Decision

_TIMEOUT_S = 5


class MutedDays(Protocol):
    async def mute(self, day: date) -> bool: ...

    async def unmute(self, day: date) -> bool: ...

    async def is_muted(self, day: date) -> bool: ...


def while_muted(decision: Decision) -> Decision:
    if decision.verdict in (Verdict.SPEAK, Verdict.NOTIFY):
        return Decision(Verdict.DIGEST, urgent=False)
    return decision


class NoArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


def mute_tools(days: MutedDays, clock: Clock) -> tuple[ToolFn, ToolFn]:
    @tool(
        "proactive.mute_today",
        Permission.L2,
        params=NoArgs,
        timeout_s=_TIMEOUT_S,
        description=(
            "今天剩下的时间不主动打扰用户：主动提醒不出声、不推送，都攒进简报，过了 0 点自动恢复。"
            '用户说"今天别打扰我""下午别烦我""开个勿扰"这类、要你今天别再主动找他时用。'
            "用户自己要的照常告诉他：闹钟、定的提醒，派出去的活做完了、有事问他"
        ),
    )
    async def mute_today(args: NoArgs, ctx: ToolContext) -> ToolResult:
        if not await days.mute(clock.now().date()):
            return ToolResult("今天本来就开着不打扰")
        return ToolResult(
            "今天不打扰了：到今晚 0 点，主动提醒不出声、不推送，都攒进简报。"
            "用户自己要的不在里面，照常告诉他：闹钟、定的提醒，派出去的活做完了、有事问他。"
            "回话时别说成什么都不提醒了",
            undo_args={},
        )

    @tool(
        "proactive.unmute",
        Permission.L2,
        params=NoArgs,
        timeout_s=_TIMEOUT_S,
        description=(
            '恢复主动提醒，取消今天的不打扰。用户说"恢复提醒""可以打扰我了"时用；'
            '当前情境里写着今天开着不打扰、用户又说"恢复"时也用。没开着时不用调'
        ),
    )
    async def unmute(args: NoArgs, ctx: ToolContext) -> ToolResult:
        if not await days.unmute(clock.now().date()):
            return ToolResult("今天本来就没开不打扰，主动提醒是照常的")
        return ToolResult("恢复了：主动提醒照常出声、推送", undo_args={})

    return mute_today, unmute
