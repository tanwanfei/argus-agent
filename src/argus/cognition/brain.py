import logging
import random
from collections.abc import AsyncGenerator, Awaitable, Iterable, Mapping, Sequence
from contextlib import aclosing
from dataclasses import dataclass
from typing import Any, Literal, Protocol, assert_never

from argus.action.executor import Denied, Executor, Failed, FailedStep, Pending
from argus.action.executor import Done as ToolDone
from argus.action.registry import ToolRegistry
from argus.cognition.context_builder import build_context
from argus.cognition.sentence_splitter import SentenceSplitter
from argus.kernel.clock import Clock
from argus.kernel.geo import GeoFix
from argus.kernel.tools import Origin, Permission, Speaker, ToolCall, ToolContext
from argus.kernel.tracing import LatencyMark, mark_latency
from argus.llm.gateway import (
    ChatMessage,
    LlmError,
    LlmRequest,
    Message,
    ModelGateway,
    Purpose,
    Reasoning,
    TextDelta,
    Tier,
    ToolCallsMessage,
    ToolResultMessage,
    Usage,
)

logger = logging.getLogger(__name__)

SPOKEN_REPLY_MAX_CHARS = 230
SPOKEN_REQUEST_MAX_TOKENS = 6000

MAX_TOOL_ROUNDS = 6
SPOKEN_TURN_TIMEOUT_S = 30.0
UNSPOKEN_TURN_TIMEOUT_S = 300.0


@dataclass(frozen=True)
class TurnInput:
    utterance: str
    history: Sequence[ChatMessage]
    situation: str
    spoken: bool
    speaker: Speaker
    origin: Origin
    precise_location: GeoFix | None


class TurnMemory(Protocol):
    async def resident_messages(self) -> Sequence[Message]: ...

    async def recall(self, turn: TurnInput) -> Sequence[Message]: ...


@dataclass(frozen=True)
class Sentence:
    text: str


@dataclass(frozen=True)
class Remember:
    text: str


@dataclass(frozen=True)
class Done:
    usages: tuple[Usage, ...]


@dataclass(frozen=True)
class NeedConfirm:
    pending: Pending
    call: ToolCall


@dataclass(frozen=True)
class ToolStarted:
    name: str
    round: int
    announce: str | None


@dataclass(frozen=True)
class ToolFinished:
    name: str
    round: int
    outcome: Literal["done", "denied", "failed"]
    elapsed_s: float


ReplyEvent = Sentence | Remember | NeedConfirm | ToolStarted | ToolFinished | Done


class Brain:
    def __init__(
        self,
        gateway: ModelGateway,
        persona: str,
        *,
        registry: ToolRegistry,
        executor: Executor,
        clock: Clock,
        memory: TurnMemory | None = None,
    ) -> None:
        self._gateway = gateway
        self._persona = persona
        self._registry = registry
        self._executor = executor
        self._clock = clock
        self._memory = memory
        self._last_closing_reply: str | None = None

    def _announce_of(self, tool_name: str) -> str | None:
        registered = self._registry.get(tool_name)
        return None if registered is None else registered.spec.announce

    def _closing_replies_of(self, tool_name: str) -> tuple[str, ...]:
        registered = self._registry.get(tool_name)
        return () if registered is None else registered.spec.closing_replies

    def _pick_closing_reply(self, replies: tuple[str, ...]) -> str:
        choices = [reply for reply in replies if reply != self._last_closing_reply] or list(replies)
        picked = random.choice(choices)
        self._last_closing_reply = picked
        return picked

    async def respond(self, turn: TurnInput) -> AsyncGenerator[ReplyEvent]:
        tool_context = ToolContext(
            speaker=turn.speaker, origin=turn.origin, precise_location=turn.precise_location
        )
        splitter = SentenceSplitter()
        spoken_length = _SpokenLength(SPOKEN_REPLY_MAX_CHARS if turn.spoken else None)
        usages: list[Usage] = []
        tool_rounds = _ToolRounds()
        waiting_time = _WaitingTime(
            self._clock, SPOKEN_TURN_TIMEOUT_S if turn.spoken else UNSPOKEN_TURN_TIMEOUT_S
        )
        try:
            resident_memory: Sequence[Message] = ()
            recalled_memory: Sequence[Message] = ()
            if self._memory is not None and turn.speaker is Speaker.OWNER:
                resident_memory = await self._memory.resident_messages()
                recalled_memory = await waiting_time.wait_for(
                    self._memory.recall(turn), doing="召回记忆"
                )
            messages: list[ChatMessage] = list(
                build_context(
                    persona=self._persona,
                    situation=turn.situation,
                    history=turn.history,
                    utterance=turn.utterance,
                    spoken=turn.spoken,
                    speaker=turn.speaker,
                    resident_memory=resident_memory,
                    recalled_memory=recalled_memory,
                )
            )
            while True:
                request = LlmRequest(
                    tier=Tier.MAIN,
                    purpose=Purpose.DIALOG,
                    max_tokens=SPOKEN_REQUEST_MAX_TOKENS if turn.spoken else None,
                    messages=tuple(messages),
                    tools=self._registry.specs(),
                    thinking=True,
                )
                said: list[str] = []
                reasoning = ""
                tool_calls: list[ToolCall] = []
                usage: Usage | None = None
                async with aclosing(self._gateway.stream(request)) as deltas:
                    while True:
                        delta = await waiting_time.wait_for(anext(deltas, None), doing="等模型回复")
                        if delta is None:
                            break
                        if isinstance(delta, TextDelta):
                            mark_latency(LatencyMark.LLM_FIRST_TOKEN)
                            said.append(delta.text)
                            if turn.spoken:
                                for sentence in spoken_length.take(splitter.feed(delta.text)):
                                    yield Sentence(sentence)
                                if spoken_length.reached:
                                    break
                        elif isinstance(delta, Reasoning):
                            reasoning = delta.text
                        elif isinstance(delta, ToolCall):
                            tool_calls.append(delta)
                        elif isinstance(delta, Usage):
                            usage = delta
                if spoken_length.reached:
                    break
                if usage is None:
                    raise LlmError("模型的流正常结束了，但没有给用量")
                usages.append(usage)
                if not tool_calls:
                    break

                for sentence in spoken_length.take(
                    _rest_of_this_call(splitter, said, spoken=turn.spoken)
                ):
                    yield Sentence(sentence)
                if spoken_length.reached:
                    break
                tool_rounds.start(tool_calls)
                round_number = tool_rounds.started
                model_spoke_first = bool("".join(said).strip())
                messages.append(ToolCallsMessage("".join(said), tuple(tool_calls), reasoning))
                closing_replies: tuple[str, ...] = ()
                for tool_call in tool_calls:
                    yield ToolStarted(
                        tool_call.name,
                        round_number,
                        None if model_spoke_first else self._announce_of(tool_call.name),
                    )
                    started_at = self._clock.monotonic()
                    outcome = await waiting_time.wait_for(
                        self._executor.execute(tool_call, tool_context),
                        doing=f"调 {tool_call.name}",
                    )
                    yield ToolFinished(
                        tool_call.name,
                        round_number,
                        _outcome_kind(outcome),
                        self._clock.monotonic() - started_at,
                    )
                    if isinstance(outcome, Pending):
                        yield NeedConfirm(outcome, tool_call)
                        yield Done(tuple(usages))
                        return
                    if isinstance(outcome, Denied) and outcome.speaker_not_recognized:
                        registered_tool = self._registry.get(tool_call.name)
                        assert registered_tool is not None
                        yield Sentence(_not_recognized_reply(registered_tool.spec.permission))
                        yield Done(tuple(usages))
                        return
                    messages.append(ToolResultMessage(tool_call.id, _outcome_text(outcome)))
                    if isinstance(outcome, ToolDone) and outcome.result.remember is not None:
                        yield Remember(outcome.result.remember)
                    if isinstance(outcome, ToolDone):
                        closing_replies = closing_replies or self._closing_replies_of(
                            tool_call.name
                        )
                if closing_replies:
                    if not model_spoke_first:
                        yield Sentence(self._pick_closing_reply(closing_replies))
                    yield Done(tuple(usages))
                    return
        except _TurnStopped as stopped:
            logger.warning("回合没办完就停了：%s", stopped.what_to_say)
            yield Sentence(stopped.what_to_say)
            yield Done(tuple(usages))
            return

        if not spoken_length.reached:
            if request.max_tokens is not None and usages[-1].output_tokens >= request.max_tokens:
                logger.info("回复说到了 max_tokens 上限 %d，丢掉没说完的半句", request.max_tokens)
            else:
                for sentence in spoken_length.take(
                    _rest_of_this_call(splitter, said, spoken=turn.spoken)
                ):
                    yield Sentence(sentence)
        if spoken_length.reached:
            logger.info("念出来的回复到了 %d 字上限，后面的不念了", SPOKEN_REPLY_MAX_CHARS)
        yield Done(tuple(usages))


def _rest_of_this_call(splitter: SentenceSplitter, said: list[str], *, spoken: bool) -> list[str]:
    if spoken:
        return splitter.flush()
    whole_text = "".join(said).strip()
    return [whole_text] if whole_text else []


class _SpokenLength:
    def __init__(self, limit: int | None) -> None:
        self._limit = limit
        self._said_chars = 0
        self.reached = False

    def take(self, sentences: Iterable[str]) -> list[str]:
        allowed: list[str] = []
        for sentence in sentences:
            if self.reached:
                break
            if self._limit is not None and self._said_chars + len(sentence) > self._limit:
                self.reached = True
                break
            self._said_chars += len(sentence)
            allowed.append(sentence)
        return allowed


class _TurnStopped(Exception):
    def __init__(self, what_to_say: str) -> None:
        super().__init__(what_to_say)
        self.what_to_say = what_to_say


class _ToolRounds:
    def __init__(self) -> None:
        self._rounds_started = 0
        self._calls_seen: list[tuple[str, Mapping[str, Any]]] = []

    @property
    def started(self) -> int:
        return self._rounds_started

    def start(self, tool_calls: Sequence[ToolCall]) -> None:
        if self._rounds_started == MAX_TOOL_ROUNDS:
            raise _TurnStopped(
                f"调了 {MAX_TOOL_ROUNDS} 轮工具还没办完，先停在这里，"
                f"最后要调的是 {tool_calls[0].name}。"
            )
        for tool_call in tool_calls:
            call_key = (tool_call.name, tool_call.args)
            if call_key in self._calls_seen:
                raise _TurnStopped(
                    f"第二次要用同样的参数调 {tool_call.name}，结果不会变，先停在这里。"
                )
            self._calls_seen.append(call_key)
        self._rounds_started += 1


class _WaitingTime:
    def __init__(self, clock: Clock, limit_s: float) -> None:
        self._clock = clock
        self._limit_s = limit_s
        self._left_s = limit_s

    async def wait_for[T](self, awaitable: Awaitable[T], *, doing: str) -> T:
        started = self._clock.monotonic()
        try:
            async with self._clock.timeout(self._left_s):
                return await awaitable
        except TimeoutError:
            raise _TurnStopped(
                f"办了 {_duration_text(self._limit_s)}还没办完，先停在这里，停在{doing}。"
            ) from None
        finally:
            self._left_s -= self._clock.monotonic() - started


def _duration_text(seconds: float) -> str:
    if seconds % 60 == 0:
        return f"{seconds // 60:g} 分钟"
    return f"{seconds:g} 秒"


def _not_recognized_reply(permission: Permission) -> str:
    action = "说" if permission is Permission.L1 else "做"
    return f"没认出是 sir 的声音，这个不能{action}。"


def _outcome_kind(
    outcome: ToolDone | Denied | Failed | Pending,
) -> Literal["done", "denied", "failed"]:
    match outcome:
        case ToolDone() | Pending():
            return "done"
        case Denied():
            return "denied"
        case Failed():
            return "failed"
        case _:
            assert_never(outcome)


def _outcome_text(outcome: ToolDone | Denied | Failed) -> str:
    match outcome:
        case ToolDone(result=result):
            return result.text
        case Denied(reason=reason):
            return f"没有执行：{reason}"
        case Failed(step=step, reason=reason, user_action=user_action):
            user_part = user_action if user_action is not None else "不用用户做什么"
            return f"没做成。失败在：{_step_name(step)}；原因：{reason}；需要用户做的：{user_part}"
        case _:
            assert_never(outcome)


def _step_name(step: FailedStep) -> str:
    match step:
        case FailedStep.CHECK_ID:
            return "核对调用 id"
        case FailedStep.LOOKUP:
            return "找工具"
        case FailedStep.VALIDATE:
            return "校验参数"
        case FailedStep.RUN:
            return "执行"
        case _:
            assert_never(step)
