import asyncio
import json
import re
from collections.abc import AsyncGenerator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import assert_never

import httpx2
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from argus.kernel.tools import ToolCall, ToolSpec
from argus.llm.gateway import (
    ChatMessage,
    CloudHealth,
    LlmDelta,
    LlmError,
    LlmRequest,
    LlmRequestInvalid,
    LlmResponse,
    Message,
    Reasoning,
    TextDelta,
    ToolCallsMessage,
    ToolResultMessage,
    Usage,
)

REQUEST_BODY_KEYS = frozenset(
    {"model", "messages", "stream", "stream_options", "max_tokens", "tools"}
)

THINKING_BODY_KEYS = frozenset({"thinking", "reasoning_effort"})


class ThinkingLevel(StrEnum):
    OFF = "off"
    LOW = "low"
    DEFAULT = "default"


_TIMEOUT = httpx2.Timeout(10.0, read=60.0)

_STREAM_END = "[DONE]"

_VENDOR_MESSAGE_LIMIT = 200

_INVALID_REQUEST_STATUSES = frozenset({400, 413, 422})

_VENDOR_NAME_SEPARATOR = "-"
_VENDOR_NAME_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}")


class OpenAICompatProvider:
    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str,
        proxy: str | None,
        extra_body: Mapping[str, object],
        thinking_level: ThinkingLevel | None = None,
        first_chunk_timeout_s: float | None = None,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self._model = model
        self._thinking_level = thinking_level
        self._first_chunk_timeout_s = first_chunk_timeout_s
        self._extra_body = dict(extra_body)
        self._client = httpx2.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            trust_env=False,
            proxy=proxy,
            timeout=_TIMEOUT,
            transport=transport,
        )

    async def complete(self, request: LlmRequest) -> LlmResponse:
        if request.tools:
            raise ValueError("complete() 不支持工具调用，要工具就用 stream()")
        body = self._request_body(request, stream=False)
        try:
            response = await self._client.post("chat/completions", json=body)
        except httpx2.TimeoutException as error:
            raise LlmError(f"等模型接口回复超时：{self._client.base_url}") from error
        except httpx2.RequestError as error:
            raise LlmError(f"连不上模型接口 {self._client.base_url}：{error!r}") from error

        _raise_for_status(response)
        return _parse_completion(response, self._model)

    async def stream(self, request: LlmRequest) -> AsyncGenerator[LlmDelta]:
        tool_names = _tool_names_by_vendor_name(request.tools)
        body = self._request_body(request, stream=True)
        tool_calls = _ToolCallAssembler(tool_names)
        reasoning_pieces: list[str] = []
        usage: Usage | None = None
        try:
            async with asyncio.timeout(self._first_chunk_timeout_s) as first_chunk_deadline:
                async with self._client.sse("chat/completions", method="POST", json=body) as events:
                    if not events.response.is_success:
                        await events.response.aread()
                        _raise_for_status(events.response)
                    async for event in events:
                        first_chunk_deadline.reschedule(None)
                        if event.data == _STREAM_END:
                            break
                        chunk = _parse_stream_chunk(event.data)
                        text = chunk.text()
                        if text:
                            yield TextDelta(text)
                        reasoning_pieces.append(chunk.reasoning())
                        tool_calls.add(chunk.tool_call_pieces())
                        if chunk.usage is not None:
                            usage = _to_usage(chunk.usage, self._model)
        except TimeoutError as error:
            raise LlmError(
                f"等了 {self._first_chunk_timeout_s:g} 秒，模型接口还没开始回复：{self._client.base_url}"
            ) from error
        except httpx2.TimeoutException as error:
            raise LlmError(f"等模型接口回复超时：{self._client.base_url}") from error
        except httpx2.RequestError as error:
            raise LlmError(f"连不上模型接口，或者读到一半断了：{error!r}") from error

        finished_tool_calls = tool_calls.finish()
        if usage is None:
            raise LlmError(
                "模型接口的流结束了，但没有给用量：厂商可能不支持 stream_options.include_usage"
            )
        reasoning_text = "".join(reasoning_pieces)
        if reasoning_text:
            yield Reasoning(reasoning_text)
        for tool_call in finished_tool_calls:
            yield tool_call
        yield usage

    def health(self) -> CloudHealth:
        return CloudHealth.UP

    async def probe(self) -> None:
        try:
            response = await self._client.get("models")
        except httpx2.TimeoutException as error:
            raise LlmError(f"等模型接口回复超时：{self._client.base_url}") from error
        except httpx2.RequestError as error:
            raise LlmError(f"连不上模型接口 {self._client.base_url}：{error!r}") from error
        _raise_for_status(response)

    async def aclose(self) -> None:
        await self._client.aclose()

    def _thinks(self, request: LlmRequest) -> bool:
        return (
            request.thinking
            and self._thinking_level is not None
            and self._thinking_level is not ThinkingLevel.OFF
        )

    def _thinking_body(self, *, thinks: bool) -> dict[str, object]:
        if self._thinking_level is None:
            return {}
        if not thinks:
            return {"thinking": {"type": "disabled"}}
        body: dict[str, object] = {"thinking": {"type": "enabled"}}
        if self._thinking_level is ThinkingLevel.LOW:
            body["reasoning_effort"] = "low"
        return body

    def _request_body(self, request: LlmRequest, *, stream: bool) -> dict[str, object]:
        thinks = self._thinks(request)
        body: dict[str, object] = {
            **self._extra_body,
            **self._thinking_body(thinks=thinks),
            "model": self._model,
            "messages": [
                _message_body(message, send_reasoning=thinks) for message in request.messages
            ],
            "stream": stream,
        }
        if stream:
            body["stream_options"] = {"include_usage": True}
        if request.max_tokens is not None:
            body["max_tokens"] = request.max_tokens
        if request.tools:
            body["tools"] = [_tool_definition(spec) for spec in request.tools]
        return body


def _message_body(message: ChatMessage, *, send_reasoning: bool) -> dict[str, object]:
    match message:
        case Message(role=role, content=content):
            return {"role": role.value, "content": content}
        case ToolCallsMessage(content=content, calls=calls, reasoning=reasoning):
            body: dict[str, object] = {
                "role": "assistant",
                "content": content or None,
                "tool_calls": [_tool_call_body(call) for call in calls],
            }
            if send_reasoning and reasoning:
                body["reasoning_content"] = reasoning
            return body
        case ToolResultMessage(call_id=call_id, content=content):
            return {"role": "tool", "tool_call_id": call_id, "content": content}
        case _:
            assert_never(message)


def _tool_call_body(call: ToolCall) -> dict[str, object]:
    return {
        "id": call.id,
        "type": "function",
        "function": {
            "name": _vendor_name(call.name),
            "arguments": json.dumps(dict(call.args), ensure_ascii=False),
        },
    }


def _vendor_name(tool_name: str) -> str:
    return tool_name.replace(".", _VENDOR_NAME_SEPARATOR)


def _tool_names_by_vendor_name(tools: Sequence[ToolSpec]) -> dict[str, str]:
    tool_names: dict[str, str] = {}
    for spec in tools:
        vendor_name = _vendor_name(spec.name)
        if not _VENDOR_NAME_PATTERN.fullmatch(vendor_name):
            raise ValueError(
                f"工具名 {spec.name} 发不出去：只能用字母、数字、_、-、.，最长 64 个字"
            )
        if vendor_name in tool_names:
            raise ValueError(
                f"工具 {tool_names[vendor_name]} 和 {spec.name} 发给厂商时撞成了同一个名字"
            )
        tool_names[vendor_name] = spec.name
    return tool_names


def _tool_definition(spec: ToolSpec) -> dict[str, object]:
    return {
        "type": "function",
        "function": {
            "name": _vendor_name(spec.name),
            "description": spec.description,
            "parameters": spec.params.model_json_schema(),
        },
    }


@dataclass
class _ToolCallParts:
    id: str | None = None
    vendor_name: str | None = None
    arguments: list[str] = field(default_factory=list)


class _ToolCallAssembler:
    def __init__(self, tool_names: Mapping[str, str]) -> None:
        self._tool_names = tool_names
        self._parts_by_index: dict[int, _ToolCallParts] = {}

    def add(self, pieces: Sequence["_StreamToolCall"]) -> None:
        for piece in pieces:
            parts = self._parts_by_index.setdefault(piece.index, _ToolCallParts())
            if piece.id and parts.id is None:
                parts.id = piece.id
            if piece.function is None:
                continue
            if piece.function.name and parts.vendor_name is None:
                parts.vendor_name = piece.function.name
            if piece.function.arguments:
                parts.arguments.append(piece.function.arguments)

    def finish(self) -> list[ToolCall]:
        tool_calls: list[ToolCall] = []
        for index in sorted(self._parts_by_index):
            parts = self._parts_by_index[index]
            if parts.id is None or parts.vendor_name is None:
                raise LlmError(f"模型接口返回的第 {index + 1} 个工具调用缺了 id 或函数名")
            name = self._tool_names.get(parts.vendor_name, parts.vendor_name)
            tool_calls.append(ToolCall(parts.id, name, _parse_arguments(name, parts.arguments)))
        return tool_calls


def _parse_arguments(tool_name: str, pieces: list[str]) -> dict[str, object]:
    error = LlmError(
        f"模型给工具 {tool_name} 的参数不是完整的 JSON 对象，可能是说到了 max_tokens 上限被截断了"
    )
    try:
        arguments = json.loads("".join(pieces))
    except json.JSONDecodeError:
        raise error from None
    if not isinstance(arguments, dict):
        raise error
    return arguments


def _raise_for_status(response: httpx2.Response) -> None:
    if response.is_success:
        return
    status = response.status_code
    if status in (401, 403):
        raise LlmError(f"模型接口拒绝了请求（HTTP {status}）：密钥不对，或者没有权限")
    message = f"模型接口返回 HTTP {status}：{_vendor_message(response)}"
    if status in _INVALID_REQUEST_STATUSES:
        raise LlmRequestInvalid(message)
    raise LlmError(message)


def _vendor_message(response: httpx2.Response) -> str:
    try:
        message = response.json()["error"]["message"]
    except (ValueError, KeyError, TypeError):
        return response.reason_phrase
    return str(message)[:_VENDOR_MESSAGE_LIMIT]


class _VendorPayload(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)


class _PromptTokensDetails(_VendorPayload):
    cached_tokens: int | None = None


class _ResponseUsage(_VendorPayload):
    prompt_tokens: int
    completion_tokens: int
    prompt_tokens_details: _PromptTokensDetails | None = None
    prompt_cache_hit_tokens: int | None = None

    def cached_input_tokens(self) -> int:
        details = self.prompt_tokens_details
        if details is not None and details.cached_tokens is not None:
            return details.cached_tokens
        if self.prompt_cache_hit_tokens is not None:
            return self.prompt_cache_hit_tokens
        return 0


class _ResponseMessage(_VendorPayload):
    content: str


class _Choice(_VendorPayload):
    message: _ResponseMessage


class _CompletionResponse(_VendorPayload):
    choices: list[_Choice] = Field(min_length=1)
    usage: _ResponseUsage


class _StreamFunction(_VendorPayload):
    name: str | None = None
    arguments: str | None = None


class _StreamToolCall(_VendorPayload):
    index: int
    id: str | None = None
    function: _StreamFunction | None = None


class _StreamDelta(_VendorPayload):
    content: str | None = None
    reasoning_content: str | None = None
    tool_calls: list[_StreamToolCall] | None = None


class _StreamChoice(_VendorPayload):
    delta: _StreamDelta


class _StreamChunk(_VendorPayload):
    choices: list[_StreamChoice] = []
    usage: _ResponseUsage | None = None

    def text(self) -> str:
        if not self.choices:
            return ""
        return self.choices[0].delta.content or ""

    def reasoning(self) -> str:
        if not self.choices:
            return ""
        return self.choices[0].delta.reasoning_content or ""

    def tool_call_pieces(self) -> list[_StreamToolCall]:
        if not self.choices:
            return []
        return self.choices[0].delta.tool_calls or []


def _parse_completion(response: httpx2.Response, model: str) -> LlmResponse:
    try:
        completion = _CompletionResponse.model_validate_json(response.content)
        usage = _to_usage(completion.usage, model)
    except ValidationError as error:
        raise LlmError(f"模型接口返回的内容格式不对：{error}") from error
    return LlmResponse(text=completion.choices[0].message.content, usage=usage)


def _parse_stream_chunk(data: str) -> _StreamChunk:
    try:
        return _StreamChunk.model_validate_json(data)
    except ValidationError as error:
        raise LlmError(f"模型接口返回的内容格式不对：{error}") from error


def _to_usage(response_usage: _ResponseUsage, model: str) -> Usage:
    try:
        return Usage(
            model=model,
            input_tokens=response_usage.prompt_tokens,
            cached_input_tokens=response_usage.cached_input_tokens(),
            output_tokens=response_usage.completion_tokens,
        )
    except ValueError as error:
        raise LlmError(f"模型接口返回的内容格式不对：{error}") from error
