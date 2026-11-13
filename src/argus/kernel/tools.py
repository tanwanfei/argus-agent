from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel

from argus.kernel.geo import GeoFix


class Permission(StrEnum):
    L1 = "read"
    L2 = "reversible"
    L3A = "send"
    L3B = "critical"


class Origin(StrEnum):
    USER = "user"
    RULE = "rule"
    PROACTIVE = "proactive"
    CODE_AGENT = "code_agent"


class Speaker(StrEnum):
    OWNER = "owner"
    OTHER = "other"


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    params: type[BaseModel]
    permission: Permission
    timeout_s: float = 10.0
    sensitive: frozenset[str] = frozenset()
    personal: bool = True
    confirm: Callable[[Any], str] | None = None
    extra_details: Callable[[Any], Awaitable[Sequence[str]]] | None = None
    announce: str | None = None
    done_reply: str | None = None

    def __post_init__(self) -> None:
        unknown_names = self.sensitive.difference(self.params.model_fields)
        if unknown_names:
            raise ValueError(
                f"工具 {self.name} 的 sensitive 里有参数模型没有的字段：{sorted(unknown_names)}"
            )


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    args: Mapping[str, Any]


@dataclass(frozen=True)
class ToolContext:
    speaker: Speaker
    origin: Origin
    precise_location: GeoFix | None


@dataclass(frozen=True)
class ToolResult:
    text: str
    remember: str | None = None
    fields: Mapping[str, str] = field(default_factory=dict)
    undo_args: Mapping[str, Any] | None = None

    @classmethod
    def ok(cls, text: str) -> Self:
        return cls(text)


class ToolFailure(Exception):
    def __init__(self, reason: str, *, user_action: str | None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.user_action = user_action


@dataclass(frozen=True)
class ToolFn:
    spec: ToolSpec
    run: Callable[[Any, ToolContext], Awaitable[ToolResult]]


def tool[ArgsT: BaseModel](
    name: str,
    permission: Permission,
    *,
    params: type[ArgsT],
    description: str,
    timeout_s: float = 10.0,
    sensitive: tuple[str, ...] = (),
    personal: bool = True,
    confirm: Callable[[ArgsT], str] | None = None,
    extra_details: Callable[[ArgsT], Awaitable[Sequence[str]]] | None = None,
    announce: str | None = None,
    done_reply: str | None = None,
) -> Callable[[Callable[[ArgsT, ToolContext], Awaitable[ToolResult]]], ToolFn]:
    spec = ToolSpec(
        name=name,
        description=description,
        params=params,
        permission=permission,
        timeout_s=timeout_s,
        sensitive=frozenset(sensitive),
        personal=personal,
        confirm=confirm,
        extra_details=extra_details,
        announce=announce,
        done_reply=done_reply,
    )

    def bind(run: Callable[[ArgsT, ToolContext], Awaitable[ToolResult]]) -> ToolFn:
        return ToolFn(spec, run)

    return bind


class ConfirmVia(StrEnum):
    VOICE = "voice"
    CARD = "card"


@dataclass(frozen=True)
class CardPrompt:
    ticket_id: str
    title: str
    expires_at: datetime


class CardDelivery(StrEnum):
    SHOWN = "shown"
    PUSHED = "pushed"
    NOT_SENT = "not_sent"


@dataclass(frozen=True)
class Confirmation:
    ticket_id: str
    via: ConfirmVia
    speaker: Speaker | None = None
    args_hash: str | None = None
    sig: bytes | None = None
    approved: bool = True
