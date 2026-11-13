import re
from collections.abc import AsyncGenerator, Mapping
from contextlib import aclosing
from dataclasses import replace
from typing import Any

from argus.kernel.tools import ToolCall
from argus.llm.gateway import (
    ChatMessage,
    CloudHealth,
    LlmDelta,
    LlmRequest,
    LlmResponse,
    Message,
    ModelGateway,
    Reasoning,
    TextDelta,
    Tier,
    ToolCallsMessage,
    ToolResultMessage,
)
from argus.llm.redaction import PLACEHOLDER_PREFIX, Placeholders, Redactor


class Redacting:
    def __init__(self, inner: ModelGateway, *, redactor: Redactor) -> None:
        self._inner = inner
        self._redactor = redactor

    def health(self) -> CloudHealth:
        return self._inner.health()

    async def probe(self) -> None:
        await self._inner.probe()

    async def complete(self, request: LlmRequest) -> LlmResponse:
        if request.tier is Tier.LOCAL:
            return await self._inner.complete(request)
        placeholders = Placeholders()
        response = await self._inner.complete(self._redact_request(request, placeholders))
        return replace(response, text=placeholders.restore(response.text))

    async def stream(self, request: LlmRequest) -> AsyncGenerator[LlmDelta]:
        if request.tier is Tier.LOCAL:
            async with aclosing(self._inner.stream(request)) as deltas:
                async for delta in deltas:
                    yield delta
            return

        placeholders = Placeholders()
        redacted_request = self._redact_request(request, placeholders)
        held_back = _HeldBackText(placeholders)
        async with aclosing(self._inner.stream(redacted_request)) as deltas:
            async for delta in deltas:
                if not placeholders:
                    yield delta
                elif isinstance(delta, TextDelta):
                    released = held_back.add(delta.text)
                    if released:
                        yield TextDelta(released)
                elif isinstance(delta, Reasoning):
                    yield delta
                elif isinstance(delta, ToolCall):
                    yield replace(delta, args=_restore_args(delta.args, placeholders))
                else:
                    rest = held_back.flush()
                    if rest:
                        yield TextDelta(rest)
                    yield delta

    def _redact_request(self, request: LlmRequest, placeholders: Placeholders) -> LlmRequest:
        messages = tuple(
            self._redact_message(message, placeholders) for message in request.messages
        )
        return replace(request, messages=messages)

    def _redact_message(self, message: ChatMessage, placeholders: Placeholders) -> ChatMessage:
        redact = self._redactor.redact
        if isinstance(message, Message):
            return replace(message, content=redact(message.content, placeholders))
        if isinstance(message, ToolCallsMessage):
            calls = tuple(
                replace(call, args=_redact_args(call.args, self._redactor, placeholders))
                for call in message.calls
            )
            return replace(message, content=redact(message.content, placeholders), calls=calls)
        return ToolResultMessage(message.call_id, redact(message.content, placeholders))


class _HeldBackText:
    def __init__(self, placeholders: Placeholders) -> None:
        self._placeholders = placeholders
        self._pending = ""

    def add(self, text: str) -> str:
        self._pending += text
        hold_from = self._pending.rfind(PLACEHOLDER_PREFIX)
        if hold_from == -1:
            return self.flush()
        tail = self._pending[hold_from:]
        if not (_could_become_placeholder(tail) or _ends_with_placeholder(tail)):
            return self.flush()
        released, self._pending = self._pending[:hold_from], self._pending[hold_from:]
        return self._placeholders.restore(released)

    def flush(self) -> str:
        released, self._pending = self._pending, ""
        return self._placeholders.restore(released)


def _could_become_placeholder(tail: str) -> bool:
    body = tail[len(PLACEHOLDER_PREFIX) :]
    kind, separator, number = body.partition("_")
    if not separator:
        return kind == "" or (kind.isascii() and kind.isupper() and kind.isalpha())
    return bool(kind) and kind.isascii() and kind.isupper() and (number == "" or number.isdigit())


_PLACEHOLDER_AT_END = re.compile(r"‹[A-Z]+_\d+› ?")


def _ends_with_placeholder(tail: str) -> bool:
    return _PLACEHOLDER_AT_END.fullmatch(tail) is not None


def _redact_args(
    args: Mapping[str, Any], redactor: Redactor, placeholders: Placeholders
) -> dict[str, Any]:
    return {key: _redact_value(value, redactor, placeholders) for key, value in args.items()}


def _restore_args(args: Mapping[str, Any], placeholders: Placeholders) -> dict[str, Any]:
    return {key: _restore_value(value, placeholders) for key, value in args.items()}


def _redact_value(value: object, redactor: Redactor, placeholders: Placeholders) -> object:
    if isinstance(value, str):
        return redactor.redact(value, placeholders)
    if isinstance(value, Mapping):
        return {key: _redact_value(item, redactor, placeholders) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_value(item, redactor, placeholders) for item in value]
    return value


def _restore_value(value: object, placeholders: Placeholders) -> object:
    if isinstance(value, str):
        return placeholders.restore(value)
    if isinstance(value, Mapping):
        return {key: _restore_value(item, placeholders) for key, item in value.items()}
    if isinstance(value, list):
        return [_restore_value(item, placeholders) for item in value]
    return value
