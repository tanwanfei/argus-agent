from dataclasses import dataclass
from enum import StrEnum


class WriteReason(StrEnum):
    EXPLICIT = "explicit"
    STABLE_FACT = "stable_fact"
    EVENT = "event"


_FACT_REASONS = frozenset({WriteReason.EXPLICIT, WriteReason.STABLE_FACT})
_EPISODE_REASONS = frozenset({WriteReason.EVENT})


@dataclass(frozen=True)
class GateDecision:
    write: bool
    explanation: str


def admit_fact(*, subject: str, attribute: str, value: str, reason: str | None) -> GateDecision:
    if not subject.strip() or not attribute.strip() or not value.strip():
        return GateDecision(False, "主体、属性、值里有空的")
    return _check_reason(reason, accepted=_FACT_REASONS, memory_kind="语义记忆")


def admit_episode(*, summary: str, reason: str | None) -> GateDecision:
    if not summary.strip():
        return GateDecision(False, "摘要是空的")
    return _check_reason(reason, accepted=_EPISODE_REASONS, memory_kind="情景记忆")


def _check_reason(
    reason: str | None, *, accepted: frozenset[WriteReason], memory_kind: str
) -> GateDecision:
    if reason is None:
        return GateDecision(False, "没说为什么值得记，按闲聊丢弃")
    try:
        write_reason = WriteReason(reason)
    except ValueError:
        return GateDecision(False, f"理由 {reason!r} 不是三种之一，按闲聊丢弃")
    if write_reason not in accepted:
        return GateDecision(False, f"理由 {reason} 不该写进{memory_kind}")
    return GateDecision(True, f"理由 {reason}")
