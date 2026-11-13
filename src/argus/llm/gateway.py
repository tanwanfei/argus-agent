from collections.abc import AsyncGenerator
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from argus.kernel.tools import ToolCall, ToolSpec


class Tier(StrEnum):
    MAIN = "main"
    FLAGSHIP = "flagship"
    LOCAL = "local"


class Purpose(StrEnum):
    DIALOG = "dialog"
    EXTRACT = "extract"
    PROACTIVE = "proactive"
    DIGEST = "digest"
    CODEAGENT = "codeagent"


class CloudHealth(StrEnum):
    UP = "up"
    DOWN = "down"


class Role(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


@dataclass(frozen=True)
class Message:
    role: Role
    content: str


@dataclass(frozen=True)
class ToolCallsMessage:
    content: str
    calls: tuple[ToolCall, ...]
    reasoning: str = ""


@dataclass(frozen=True)
class ToolResultMessage:
    call_id: str
    content: str


ChatMessage = Message | ToolCallsMessage | ToolResultMessage


@dataclass(frozen=True)
class LlmRequest:
    tier: Tier
    purpose: Purpose
    messages: tuple[ChatMessage, ...]
    tools: tuple[ToolSpec, ...] = ()
    max_tokens: int | None = None
    thinking: bool = False

    def __post_init__(self) -> None:
        if not self.messages:
            raise ValueError("LlmRequest.messages 不能为空")
        if self.max_tokens is not None and self.max_tokens < 1:
            raise ValueError(f"LlmRequest.max_tokens 必须 ≥ 1 或为 None，收到 {self.max_tokens}")


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class Usage:
    model: str
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int

    def __post_init__(self) -> None:
        if not 0 <= self.cached_input_tokens <= self.input_tokens:
            raise ValueError(
                f"Usage.cached_input_tokens 必须在 0 到 input_tokens 之间，"
                f"收到 {self.cached_input_tokens}（input_tokens={self.input_tokens}）"
            )


@dataclass(frozen=True)
class Reasoning:
    text: str


LlmDelta = TextDelta | Reasoning | ToolCall | Usage


@dataclass(frozen=True)
class LlmResponse:
    text: str
    usage: Usage


class LlmError(Exception):
    pass


class LlmRequestInvalid(LlmError):
    pass


class ModelGateway(Protocol):
    def stream(self, request: LlmRequest) -> AsyncGenerator[LlmDelta]: ...

    async def complete(self, request: LlmRequest) -> LlmResponse: ...

    def health(self) -> CloudHealth: ...

    async def probe(self) -> None: ...
