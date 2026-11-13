import asyncio
import logging
import traceback
from collections import OrderedDict
from collections.abc import Coroutine
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import Any, assert_never

from pydantic import BaseModel, ValidationError

from argus.action.registry import ToolRegistry
from argus.governance.governance import (
    NOT_RECOGNIZED_REASON,
    Allow,
    AllowWithUndo,
    AuditResult,
    ConfirmByCard,
    ConfirmByVoice,
    Decision,
    Deny,
    Governance,
)
from argus.governance.tickets import ConfirmationRejected, Ticket, Tickets, args_hash
from argus.kernel.clock import Clock
from argus.kernel.tools import (
    Confirmation,
    ConfirmVia,
    Permission,
    ToolCall,
    ToolContext,
    ToolFailure,
    ToolFn,
    ToolResult,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Done:
    result: ToolResult
    done_reply: str | None = None


@dataclass(frozen=True)
class Denied:
    reason: str

    @property
    def speaker_not_recognized(self) -> bool:
        return self.reason == NOT_RECOGNIZED_REASON


class FailedStep(StrEnum):
    CHECK_ID = "check_id"
    LOOKUP = "lookup"
    VALIDATE = "validate"
    RUN = "run"


@dataclass(frozen=True)
class Failed:
    step: FailedStep
    reason: str
    user_action: str | None


@dataclass(frozen=True)
class Pending:
    ticket: Ticket
    confirm_text: str
    details: tuple[str, ...]


ToolOutcome = Done | Pending | Denied | Failed


@dataclass(frozen=True)
class Recalled:
    pass


_REMEMBERED_CALLS = 256

UNDO_WINDOW_SECONDS = 600
_UNDO_STACK_LIMIT = 10

RECALL_WINDOW_SECONDS = 10


@dataclass(frozen=True)
class _RememberedCall:
    call: ToolCall
    ctx: ToolContext
    outcome: ToolOutcome


@dataclass(frozen=True)
class _Undoable:
    undo: ToolFn
    call: ToolCall
    args: BaseModel
    done_at: float


@dataclass(frozen=True)
class _PendingCall:
    fn: ToolFn
    args: BaseModel
    call: ToolCall
    ctx: ToolContext
    decision: Decision
    expires_at: datetime


class Executor:
    def __init__(
        self, *, registry: ToolRegistry, governance: Governance, tickets: Tickets, clock: Clock
    ) -> None:
        self._registry = registry
        self._governance = governance
        self._tickets = tickets
        self._clock = clock
        self._pending: dict[str, _PendingCall] = {}
        self._recall_windows: list[asyncio.Event] = []
        self._remembered: OrderedDict[str, _RememberedCall] = OrderedDict()
        self._undo_stack: list[_Undoable] = []
        self._side_effect_tasks: set[asyncio.Task[Done | Failed]] = set()

    async def execute(self, call: ToolCall, ctx: ToolContext) -> ToolOutcome:
        remembered = self._remembered.get(call.id)
        if remembered is not None:
            if remembered.call != call or remembered.ctx != ctx:
                logger.warning("调用 id 和之前的一次重复，但工具、参数或情境不同：%s", call.name)
                return Failed(
                    FailedStep.CHECK_ID,
                    "这个调用 id 之前用过，工具、参数或情境和那次不同，没有执行",
                    user_action=None,
                )
            return remembered.outcome

        outcome = await self._execute_once(call, ctx)
        self._remembered[call.id] = _RememberedCall(call, ctx, outcome)
        if len(self._remembered) > _REMEMBERED_CALLS:
            self._remembered.popitem(last=False)
        return outcome

    async def _execute_once(self, call: ToolCall, ctx: ToolContext) -> ToolOutcome:
        fn = self._registry.get(call.name)
        if fn is None:
            logger.warning("模型调了没有注册的工具：%s", call.name)
            return Failed(FailedStep.LOOKUP, f"没有这个工具：{call.name}", user_action=None)

        try:
            args = fn.spec.params.model_validate(call.args)
        except ValidationError as error:
            logger.warning("工具 %s 的参数没通过校验", call.name)
            return Failed(
                FailedStep.VALIDATE,
                f"参数不对：{_describe_validation_errors(error)}",
                user_action=None,
            )

        decision = self._governance.authorize(fn.spec, call, ctx)
        await self._governance.audit_decision(fn.spec, call, ctx, decision)
        match decision:
            case Allow():
                return await self._run(fn, args, call, ctx, decision)
            case AllowWithUndo():
                return await self._run_side_effect(self._run(fn, args, call, ctx, decision))
            case Deny(reason=reason):
                return Denied(reason)
            case ConfirmByVoice() | ConfirmByCard():
                return await self._open_ticket(fn, args, call, ctx, decision)
            case _:
                assert_never(decision)

    async def _open_ticket(
        self, fn: ToolFn, args: BaseModel, call: ToolCall, ctx: ToolContext, decision: Decision
    ) -> ToolOutcome:
        confirm = fn.spec.confirm
        if confirm is None:
            raise AssertionError(f"工具 {fn.spec.name} 是 L3，却没有确认模板")
        extra_details: tuple[str, ...] = ()
        try:
            confirm_text = confirm(args)
            if fn.spec.extra_details is not None:
                extra_details = tuple(await fn.spec.extra_details(args))
        except ToolFailure as failure:
            logger.warning("工具 %s 开确认单前报告办不了", fn.spec.name)
            return Failed(FailedStep.RUN, failure.reason, user_action=failure.user_action)
        except Exception as error:
            logger.error(
                "工具 %s 的确认模板出错：%s\n%s",
                fn.spec.name,
                type(error).__name__,
                _where_it_failed(error),
            )
            return Failed(
                FailedStep.RUN, "确认的话生成不了", user_action="日志里有这个工具出错的位置，看一下"
            )
        ticket = await self._tickets.open(fn.spec, call)
        now = self._clock.now()
        self._pending = {
            ticket_id: pending
            for ticket_id, pending in self._pending.items()
            if pending.expires_at > now
        }
        self._pending[ticket.id] = _PendingCall(fn, args, call, ctx, decision, ticket.expires_at)
        return Pending(ticket, confirm_text, extra_details + _confirm_details(fn, args))

    async def execute_confirmed(self, c: Confirmation) -> Done | Denied | Failed | Recalled:
        pending = self._pending.get(c.ticket_id)
        if pending is None:
            return Failed(
                FailedStep.LOOKUP,
                "这张确认单要办的事不在了，可能主进程重启过",
                user_action="要办的话再说一遍",
            )
        try:
            ticket = await self._tickets.redeem(c)
        except ConfirmationRejected as rejected:
            return Denied(str(rejected))
        del self._pending[c.ticket_id]
        if ticket.args_hash != args_hash(pending.call.name, pending.call.args):
            logger.error("工具 %s 要执行的和确认的不一样，不执行", pending.call.name)
            return Denied("要执行的和确认的不一样")
        if (
            ticket.permission is Permission.L3A
            and c.via is ConfirmVia.VOICE
            and await self._recalled_within_window()
        ):
            await self._governance.audit_result(
                pending.fn.spec,
                pending.call,
                pending.ctx,
                pending.decision,
                AuditResult.RECALLED,
                duration_ms=0,
                confirmed_via=c.via,
            )
            return Recalled()
        outcome = await self._run_side_effect(
            self._run(
                pending.fn,
                pending.args,
                pending.call,
                pending.ctx,
                pending.decision,
                confirmed_via=c.via,
            )
        )
        if isinstance(outcome, Done):
            done_reply = pending.fn.spec.done_reply
            if done_reply is not None:
                done_reply = done_reply.format_map(outcome.result.fields)
            return replace(outcome, done_reply=done_reply)
        return outcome

    def can_recall(self) -> bool:
        return bool(self._recall_windows)

    def recall_pending(self) -> bool:
        if not self._recall_windows:
            return False
        self._recall_windows.pop().set()
        return True

    async def _recalled_within_window(self) -> bool:
        recalled = asyncio.Event()
        self._recall_windows.append(recalled)
        try:
            async with self._clock.timeout(RECALL_WINDOW_SECONDS):
                await recalled.wait()
        except TimeoutError:
            pass
        finally:
            if recalled in self._recall_windows:
                self._recall_windows.remove(recalled)
        return recalled.is_set()

    def can_undo(self) -> bool:
        return bool(self._undo_stack) and self._is_fresh(self._undo_stack[-1])

    async def undo_last(self, ctx: ToolContext) -> Done | Failed:
        self._undo_stack = [undoable for undoable in self._undo_stack if self._is_fresh(undoable)]
        if not self._undo_stack:
            return Failed(FailedStep.LOOKUP, "没有 10 分钟内做的、能撤销的事", user_action=None)
        undoable = self._undo_stack.pop()
        decision = Allow()
        await self._governance.audit_decision(undoable.undo.spec, undoable.call, ctx, decision)
        return await self._run_side_effect(
            self._run(undoable.undo, undoable.args, undoable.call, ctx, decision)
        )

    def _is_fresh(self, undoable: _Undoable) -> bool:
        return self._clock.monotonic() - undoable.done_at <= UNDO_WINDOW_SECONDS

    async def _run_side_effect(self, run: Coroutine[Any, Any, Done | Failed]) -> Done | Failed:
        task = asyncio.create_task(run, name="side-effect-tool")
        self._side_effect_tasks.add(task)
        task.add_done_callback(self._side_effect_tasks.discard)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            task.add_done_callback(_log_abandoned_failure)
            raise

    async def _run(
        self,
        fn: ToolFn,
        args: BaseModel,
        call: ToolCall,
        ctx: ToolContext,
        decision: Decision,
        *,
        confirmed_via: ConfirmVia | None = None,
    ) -> Done | Failed:
        spec = fn.spec
        started = self._clock.monotonic()
        outcome: Done | Failed
        try:
            async with self._clock.timeout(spec.timeout_s):
                result = await fn.run(args, ctx)
        except TimeoutError:
            logger.warning("工具 %s 超过 %s 秒没有结果", spec.name, spec.timeout_s)
            outcome = Failed(
                FailedStep.RUN, f"超过 {spec.timeout_s:g} 秒没有结果", user_action="过一会儿再试"
            )
        except ToolFailure as failure:
            logger.warning("工具 %s 报告没做成", spec.name)
            outcome = Failed(FailedStep.RUN, failure.reason, user_action=failure.user_action)
        except Exception as error:
            logger.error(
                "工具 %s 执行出错：%s\n%s", spec.name, type(error).__name__, _where_it_failed(error)
            )
            outcome = Failed(
                FailedStep.RUN,
                "执行出错",
                user_action="日志里有这个工具出错的位置，看一下",
            )
        else:
            outcome = Done(result)

        if isinstance(decision, AllowWithUndo) and isinstance(outcome, Done):
            self._push_undo(call, outcome.result)
        duration_ms = round((self._clock.monotonic() - started) * 1000)
        audit_result = AuditResult.DONE if isinstance(outcome, Done) else AuditResult.FAILED
        await self._governance.audit_result(
            spec, call, ctx, decision, audit_result, duration_ms, confirmed_via=confirmed_via
        )
        return outcome

    def _push_undo(self, call: ToolCall, result: ToolResult) -> None:
        if result.undo_args is None:
            return
        undo = self._registry.undo_of(call.name)
        if undo is None:
            logger.error("工具 %s 没有撤销工具，这次撤不了", call.name)
            return
        try:
            undo_args = undo.spec.params.model_validate(result.undo_args)
        except ValidationError:
            logger.error(
                "工具 %s 给的撤销参数没通过 %s 的校验，这次撤不了", call.name, undo.spec.name
            )
            return
        undo_call = ToolCall(f"{call.id}.undo", undo.spec.name, result.undo_args)
        self._undo_stack.append(_Undoable(undo, undo_call, undo_args, self._clock.monotonic()))
        del self._undo_stack[:-_UNDO_STACK_LIMIT]


def _confirm_details(fn: ToolFn, args: BaseModel) -> tuple[str, ...]:
    lines: list[str] = []
    for name, field_info in type(args).model_fields.items():
        value = getattr(args, name)
        if value is None:
            continue
        label = field_info.title or name
        shown = "***" if name in fn.spec.sensitive else str(value)
        lines.append(f"{label}：{shown}")
    return tuple(lines)


def _log_abandoned_failure(task: asyncio.Task[Done | Failed]) -> None:
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        logger.error("打断后接着跑完的工具调用出错了", exc_info=error)


def _describe_validation_errors(error: ValidationError) -> str:
    problems: list[str] = []
    for problem in error.errors(include_url=False, include_input=False):
        field_path = ".".join(str(part) for part in problem["loc"]) or "参数"
        problems.append(f"{field_path}：{problem['msg']}")
    return "；".join(problems)


def _where_it_failed(error: BaseException) -> str:
    return "".join(traceback.format_tb(error.__traceback__))
