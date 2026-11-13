from collections.abc import Mapping, Set
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

from argus.kernel.clock import Clock
from argus.kernel.tools import (
    ConfirmVia,
    Origin,
    Permission,
    Speaker,
    ToolCall,
    ToolContext,
    ToolSpec,
)
from argus.kernel.tracing import current_trace_id

_NOT_OPEN_YET = "暂未开放"

_MASKED = "***"


@dataclass(frozen=True)
class Allow:
    pass


@dataclass(frozen=True)
class AllowWithUndo:
    pass


@dataclass(frozen=True)
class ConfirmByVoice:
    pass


@dataclass(frozen=True)
class ConfirmByCard:
    pass


@dataclass(frozen=True)
class Deny:
    reason: str


Decision = Allow | AllowWithUndo | ConfirmByVoice | ConfirmByCard | Deny

NOT_RECOGNIZED_REASON = "没认出是你"
_SPEAKER_RULE_ORIGINS = frozenset({Origin.USER, Origin.RULE})
_ALLOWED_FOR_OTHER = frozenset({Permission.L1, Permission.L2})

_PROACTIVE_SUGGEST_INSTEAD = "主动任务不能直接做这件事，可以改成建议，问用户要不要做"
_PROACTIVE_NEVER = "主动任务不能做这件事"
_PROACTIVE_ACTS_FROM_STAGE = 4

_MATRIX: Mapping[tuple[Origin, Permission], Decision] = {
    (Origin.USER, Permission.L1): Allow(),
    (Origin.USER, Permission.L2): AllowWithUndo(),
    (Origin.USER, Permission.L3A): ConfirmByVoice(),
    (Origin.USER, Permission.L3B): ConfirmByCard(),
    (Origin.RULE, Permission.L1): Allow(),
    (Origin.RULE, Permission.L2): AllowWithUndo(),
    (Origin.RULE, Permission.L3A): ConfirmByVoice(),
    (Origin.RULE, Permission.L3B): ConfirmByCard(),
    (Origin.PROACTIVE, Permission.L1): Allow(),
    (Origin.PROACTIVE, Permission.L2): Deny(_PROACTIVE_SUGGEST_INSTEAD),
    (Origin.PROACTIVE, Permission.L3A): Deny(_PROACTIVE_SUGGEST_INSTEAD),
    (Origin.PROACTIVE, Permission.L3B): Deny(_PROACTIVE_NEVER),
    (Origin.CODE_AGENT, Permission.L1): Allow(),
    (Origin.CODE_AGENT, Permission.L2): Allow(),
    (Origin.CODE_AGENT, Permission.L3A): Deny("编码代理不能做要语音确认的事"),
    (Origin.CODE_AGENT, Permission.L3B): Deny("编码代理不能做要卡片确认的事"),
}


class AuditResult(StrEnum):
    DONE = "done"
    FAILED = "failed"
    RECALLED = "recalled"


@dataclass(frozen=True)
class AuditEntry:
    ts: datetime
    trace_id: str | None
    call_id: str
    origin: Origin
    speaker: Speaker
    tool: str
    args: Mapping[str, Any]
    decision: Decision
    result: AuditResult | None
    duration_ms: int | None
    confirmed_via: ConfirmVia | None = None


class AuditLog(Protocol):
    async def append(self, entry: AuditEntry) -> None: ...


class AuditWriteFailed(Exception):
    def __init__(self, message: str, *, after_running: bool) -> None:
        super().__init__(message)
        self.after_running = after_running


class Governance:
    def __init__(self, *, audit_log: AuditLog, clock: Clock, proactive_stage: int = 1) -> None:
        self._audit_log = audit_log
        self._clock = clock
        self._matrix = _MATRIX
        if proactive_stage >= _PROACTIVE_ACTS_FROM_STAGE:
            self._matrix = {**_MATRIX, (Origin.PROACTIVE, Permission.L2): AllowWithUndo()}

    def authorize(self, spec: ToolSpec, call: ToolCall, ctx: ToolContext) -> Decision:
        if (
            ctx.origin in _SPEAKER_RULE_ORIGINS
            and ctx.speaker is Speaker.OTHER
            and (spec.personal or spec.permission not in _ALLOWED_FOR_OTHER)
        ):
            return Deny(NOT_RECOGNIZED_REASON)
        return self._matrix.get((ctx.origin, spec.permission), Deny(_NOT_OPEN_YET))

    async def audit_decision(
        self, spec: ToolSpec, call: ToolCall, ctx: ToolContext, decision: Decision
    ) -> None:
        entry = self._entry(spec, call, ctx, decision, result=None, duration_ms=None)
        await self._append(entry, after_running=False)

    async def audit_result(
        self,
        spec: ToolSpec,
        call: ToolCall,
        ctx: ToolContext,
        decision: Decision,
        result: AuditResult,
        duration_ms: int,
        *,
        confirmed_via: ConfirmVia | None = None,
    ) -> None:
        entry = self._entry(spec, call, ctx, decision, result=result, duration_ms=duration_ms)
        entry = replace(entry, confirmed_via=confirmed_via)
        await self._append(entry, after_running=True)

    async def _append(self, entry: AuditEntry, *, after_running: bool) -> None:
        line = "结果" if after_running else "判定"
        try:
            await self._audit_log.append(entry)
        except Exception as error:
            raise AuditWriteFailed(
                f"{entry.tool} 的{line}那一行没写进审计", after_running=after_running
            ) from error

    def _entry(
        self,
        spec: ToolSpec,
        call: ToolCall,
        ctx: ToolContext,
        decision: Decision,
        *,
        result: AuditResult | None,
        duration_ms: int | None,
    ) -> AuditEntry:
        return AuditEntry(
            ts=self._clock.now(),
            trace_id=current_trace_id(),
            call_id=call.id,
            origin=ctx.origin,
            speaker=ctx.speaker,
            tool=spec.name,
            args=_mask_sensitive(call.args, spec.sensitive),
            decision=decision,
            result=result,
            duration_ms=duration_ms,
        )


def _mask_sensitive(args: Mapping[str, Any], sensitive_names: Set[str]) -> dict[str, Any]:
    return {name: _MASKED if name in sensitive_names else value for name, value in args.items()}
