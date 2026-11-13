import asyncio
import logging
from collections import deque
from collections.abc import AsyncGenerator, Collection
from contextlib import aclosing
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Protocol, assert_never

from argus.action.executor import Denied, Done, Executor, Failed, Pending, Recalled
from argus.cognition.brain import (
    Brain,
    NeedConfirm,
    Remember,
    Sentence,
    ToolFinished,
    ToolStarted,
    TurnInput,
)
from argus.delivery.delivery import Delivery, PreparedSpeech, SpokenReply
from argus.delivery.outbound import Outbound
from argus.governance.governance import AuditWriteFailed
from argus.kernel.clock import Clock
from argus.kernel.dialog import DialogState
from argus.kernel.events import (
    EventBus,
    MemoryForgotten,
    ProactiveDelivered,
    RouteKind,
    TurnCompleted,
    TurnRecorded,
)
from argus.kernel.feedback import Feedback
from argus.kernel.geo import GeoFix
from argus.kernel.ids import IdGen
from argus.kernel.tools import (
    CardDelivery,
    CardPrompt,
    Confirmation,
    ConfirmVia,
    Origin,
    Permission,
    Speaker,
    ToolCall,
    ToolContext,
)
from argus.kernel.tracing import (
    LatencyMark,
    LatencyMarks,
    mark_latency,
    recording_latency,
    trace,
)
from argus.llm.gateway import LlmError, Role
from argus.routing.intents import IntentMatch, builtin_fields
from argus.routing.router import (
    CloudTurn,
    Control,
    ControlKind,
    Defer,
    Route,
    RouteContext,
    Router,
    RuleHit,
)
from argus.session.deferred import DeferredTurns
from argus.session.dialog import DialogEvent, DialogStateMachine
from argus.session.end_conversation import END_CONVERSATION_TOOL
from argus.session.history import CardState, ChatHistoryStore, ChatTurn, Via
from argus.session.store import SessionStore, WorkingMessage, mentions_topic
from argus.session.working_memory import WINDOW, WorkingMemory, reply_to_remember
from argus.situation.situation import Situation
from argus.situation.summary import situation_summary
from argus.speech.inference import TtsError
from argus.speech.pipeline import Utterance

logger = logging.getLogger(__name__)

_HOME_FIX_ACCURACY_M = 1000.0

OPEN_WINDOW_SECONDS = 30

MODEL_FAILED_REPLY = "模型出错了，这句没答完，过会儿再问我吧。"

AUDIT_FAILED_REPLY = "审计记不下来，这件事先不办了，日志里有原因。"
AUDIT_FAILED_AFTER_RUNNING_REPLY = "这件事办了，但审计记不下来，先停在这里，日志里有原因。"

UNDO_DONE_REPLY = "撤销了。"

CONFIRMED_REPLY = "好，10 秒后办，要撤回就说撤回。"
RECALLED_REPLY = "撤回了，没办。"
NOTHING_TO_RECALL_REPLY = "现在没有可以撤回的事。"
CONFIRM_CANCELLED_REPLY = "好，不办了。"
CONFIRMED_DONE_REPLY = "办好了。"
CONFIRMED_PUSH_TITLE = "确认的事"

CARD_REJECTED_REPLY = "操作被拒绝了。"

CARD_SENT_REPLY = "需要你在手机上确认。"
CARD_NOT_SENT_REPLY = "这件事要在手机上确认，可手机现在批不了：App 没连着，或者还没配对。"
OTHER_CONFIRM_CARD_SENT_REPLY = "在手机上确认一下。"
OTHER_CONFIRM_CARD_NOT_SENT_REPLY = "没认出是你的声音，手机上也批不了。请你再说一遍确认。"

FILLER_AFTER_SECONDS = 2
FILLER_REPLY = "稍等。"
PROGRESS_AFTER_SECONDS = 8
PROGRESS_REPLY = "还在查。"

NAME_ONLY_REPLY = "我在。"
NAME_ONLY_RETRY_SECONDS = 30

RULE_FAILED_REPLY = "没办成：{reason}。"
RULE_DENIED_REPLY = "这件事办不了：{reason}。"
RULE_TEMPLATE_BROKEN_REPLY = "办好了，但回复模板和工具对不上，念不了结果，日志里有原因。"

DEFER_REPLY = "现在连不上，这件事我先记下来，恢复后处理。"
DEFER_NOT_OWNER_REPLY = "现在连不上，过会儿再问我吧。"
DEFER_FAILED_REPLY = "现在连不上，也没记下来，过会儿再问我吧。"

DELIVERED_NOTE = "（这条不是用户说的：下一条是你主动告诉用户的提醒）"
CONFIRMED_OUTCOME_NOTE = (
    "（这条不是用户说的：用户确认了的事主机去办了，下一条是主机告诉用户的结果）"
)
CARD_REJECTED_NOTE = (
    "（这条不是用户说的：用户在手机卡片上拒绝了，事情没办，下一条是主机回用户的话）"
)
_RECENTLY_FORGOTTEN_KEPT = 5

USELESS_LOWERED_REPLY = "好，这类提醒以后少说。"
USELESS_STOPPED_REPLY = "好，这类提醒以后不说了，你已经说了 3 次没用。"
USELESS_NOT_SAVED_REPLY = "没记下来，过会儿再说一次吧。"
USELESS_NOT_OWNER_REPLY = "没认出是你的声音，这次不算。"
USELESS_ASKED_FOR_REPLY = "这是你自己要的提醒，不会少提醒。"


def _route_kind(route: Route) -> RouteKind:
    match route:
        case RuleHit():
            return RouteKind.RULE
        case Control():
            return RouteKind.CONTROL
        case CloudTurn():
            return RouteKind.CLOUD
        case Defer():
            return RouteKind.DEFER
        case _:
            assert_never(route)


def _not_done_reply(outcome: Denied | Failed) -> str:
    match outcome:
        case Denied(reason=reason):
            return RULE_DENIED_REPLY.format(reason=reason)
        case Failed(reason=reason, user_action=user_action):
            reply = RULE_FAILED_REPLY.format(reason=reason)
            return reply if user_action is None else f"{reply}{user_action}。"
        case _:
            assert_never(outcome)


class UselessFeedback(Protocol):
    async def mark_useless(self, trigger_id: str) -> Feedback: ...


class MuteState(Protocol):
    async def is_muted(self, day: date) -> bool: ...


class WakeControl(Protocol):
    def set_wake_required(self, required: bool) -> None: ...


class _Endpoint:
    def __init__(
        self,
        endpoint_id: str,
        wake_control: WakeControl,
        *,
        exclusive: bool,
        owner_only: bool,
        name_only_after_stop: bool,
    ) -> None:
        self.endpoint_id = endpoint_id
        self.wake_control = wake_control
        self.exclusive = exclusive
        self.owner_only = owner_only
        self.name_only_after_stop = name_only_after_stop
        self.asleep = False
        self.dialog = DialogStateMachine()
        self.window_timer: asyncio.Task[None] | None = None
        self.ignoring_this_utterance = False
        self.heard_owner_early = False


@dataclass(frozen=True)
class _ForegroundTurn:
    endpoint_id: str
    voice_endpoint: _Endpoint | None
    task: asyncio.Task[None]


class _EndOfReply:
    pass


class _SilenceTooLong:
    pass


_END_OF_REPLY = _EndOfReply()
_SILENCE_TOO_LONG = _SilenceTooLong()


@dataclass
class _AddedSpeech:
    positions: set[int] = field(default_factory=set)


@dataclass
class _RouteTaken:
    kind: RouteKind | None = None
    tools_called: list[str] = field(default_factory=list)
    ends_conversation: bool = False
    confirm_call: ToolCall | None = None
    card: CardPrompt | None = None


@dataclass(frozen=True)
class _HeardLine:
    utterance: str
    speaker: Speaker
    started_at: datetime
    spoken: bool
    endpoint_id: str
    heard_wake_word: bool = False


class Session:
    def __init__(
        self,
        clock: Clock,
        ids: IdGen,
        bus: EventBus,
        brain: Brain,
        delivery: Delivery,
        *,
        router: Router,
        executor: Executor,
        deferred: DeferredTurns,
        store: SessionStore,
        history: ChatHistoryStore,
        situation: Situation,
        feedback: UselessFeedback,
        mute_state: MuteState,
        home_latitude: float,
        home_longitude: float,
    ) -> None:
        self._clock = clock
        self._ids = ids
        self._bus = bus
        self._brain = brain
        self._delivery = delivery
        self._router = router
        self._executor = executor
        self._deferred = deferred
        self._store = store
        self._history = history
        self._situation = situation
        self._feedback = feedback
        self._mute_state = mute_state
        self._home_latitude = home_latitude
        self._home_longitude = home_longitude
        self._working_memory = WorkingMemory()
        self._endpoints: dict[str, _Endpoint] = {}
        self._task_group: asyncio.TaskGroup | None = None
        self._turn: _ForegroundTurn | None = None
        self._name_only_reply: PreparedSpeech | None = None
        self._awaiting: Pending | None = None
        self._just_delivered: Outbound | None = None
        self._recently_forgotten: deque[tuple[str, datetime]] = deque(
            maxlen=_RECENTLY_FORGOTTEN_KEPT
        )

    def add_endpoint(
        self,
        endpoint_id: str,
        wake_control: WakeControl,
        *,
        exclusive: bool = False,
        owner_only: bool = False,
        name_only_after_stop: bool = False,
    ) -> None:
        if endpoint_id in self._endpoints:
            raise ValueError(f"收音端点 {endpoint_id} 已经登记过了")
        endpoint = _Endpoint(
            endpoint_id,
            wake_control,
            exclusive=exclusive,
            owner_only=owner_only,
            name_only_after_stop=name_only_after_stop,
        )
        self._endpoints[endpoint_id] = endpoint
        wake_control.set_wake_required(_needs_name(endpoint))
        self._delivery.on_state(endpoint_id, DialogState.IDLE)

    async def remove_endpoint(self, endpoint_id: str) -> None:
        endpoint = self._endpoints.pop(endpoint_id)
        turn = self._turn
        if turn is not None and turn.voice_endpoint is endpoint and not turn.task.done():
            logger.info("端点 %s 下线时回合还在答它，取消这个回合", endpoint_id)
            await self._cancel_turn(turn)
        if endpoint.window_timer is not None:
            endpoint.window_timer.cancel()
            endpoint.window_timer = None
        self._delivery.on_endpoint_removed(endpoint_id)

    async def restore_working_memory(self) -> None:
        since = self._clock.now() - WINDOW
        try:
            messages = await self._store.load(since=since)
            self._working_memory.restore(messages)
        except Exception:
            logger.exception("工作记忆没从 ses_message 读回来，从空的开始")
            return
        logger.info("工作记忆从库里读回 %d 轮", len(messages) // 2)

    async def on_memory_forgotten(self, event: MemoryForgotten) -> None:
        self._recently_forgotten.append((event.topic, event.ts))
        removed_count = self._working_memory.forget(event.topic)
        try:
            await self._store.forget(event.topic)
        except Exception:
            logger.exception("忘掉时 ses_message 没清成功，重启后工作记忆里可能又有这个话题")
        logger.info("忘掉时清掉了工作记忆里的 %d 轮", removed_count)

    async def on_proactive_delivered(self, event: ProactiveDelivered) -> None:
        turn = ChatTurn(event.ts, self._ids.new("trace"), "", event.text, Via.TYPED)
        await self._save_to_history(turn)

    async def prepare_name_only_reply(self) -> None:
        if not self._endpoints:
            return
        failed_before = False
        while True:
            try:
                self._name_only_reply = await self._delivery.prepare(NAME_ONLY_REPLY)
            except TtsError as error:
                if not failed_before:
                    logger.warning(
                        '只喊名字时回的"我在"没合成出来，%d 秒后再试，这期间只喊名字时不出声：%s',
                        NAME_ONLY_RETRY_SECONDS,
                        error,
                    )
                    failed_before = True
                await self._clock.sleep(NAME_ONLY_RETRY_SECONDS)
                continue
            if failed_before:
                logger.info('只喊名字时回的"我在"补合成好了')
            return

    async def run(self) -> None:
        async with asyncio.TaskGroup() as task_group:
            self._task_group = task_group
            try:
                never_set = asyncio.Event()
                await never_set.wait()
            finally:
                self._task_group = None

    def dialog_state(self, endpoint_id: str) -> DialogState:
        return self._endpoints[endpoint_id].dialog.state

    async def on_speech_start(self, endpoint_id: str) -> None:
        endpoint = self._endpoints[endpoint_id]
        exclusive_endpoint_id = self._exclusive_endpoint_other_than(endpoint)
        endpoint.ignoring_this_utterance = exclusive_endpoint_id is not None
        if endpoint.ignoring_this_utterance:
            logger.info(
                "端点 %s 的这句不理：%s 在线，同一时刻只认一个收音入口",
                endpoint_id,
                exclusive_endpoint_id,
            )
            return
        if endpoint.owner_only:
            return
        await self._speech_started(endpoint)

    async def _speech_started(self, endpoint: _Endpoint) -> None:
        self._handle(endpoint, DialogEvent.SPEECH_STARTED)
        turn = self._turn
        if turn is not None and turn.voice_endpoint is endpoint and not turn.task.done():
            await self._cancel_turn(turn)
        else:
            await self._delivery.stop_saying_when_quiet(endpoint.endpoint_id)

    async def on_barge_in(self, endpoint_id: str) -> None:
        endpoint = self._endpoints[endpoint_id]
        if endpoint.dialog.state != DialogState.SPEAKING:
            logger.info("端点 %s 报了打断，这时没在念回复，不用停", endpoint_id)
            return
        self._handle(endpoint, DialogEvent.BARGED_IN)
        turn = self._turn
        if turn is not None and turn.voice_endpoint is endpoint and not turn.task.done():
            await self._cancel_turn(turn)

    async def on_owner_heard(self, endpoint_id: str) -> None:
        endpoint = self._endpoints[endpoint_id]
        if endpoint.ignoring_this_utterance or not endpoint.owner_only or endpoint.asleep:
            return
        endpoint.heard_owner_early = True
        logger.info("端点 %s 说到一半认出是你，先停下听你说", endpoint_id)
        await self._speech_started(endpoint)

    async def on_utterance(self, u: Utterance) -> None:
        endpoint = self._endpoints[u.endpoint_id]
        if endpoint.ignoring_this_utterance:
            return
        if endpoint.owner_only:
            woken_by_name = endpoint.asleep and u.heard_wake_word
            started_early = endpoint.heard_owner_early
            endpoint.heard_owner_early = False
            if u.speaker is not Speaker.OWNER and not self._asks_to_stop(u) and not woken_by_name:
                logger.info("端点 %s 的这句没认出是你，不理", u.endpoint_id)
                self._delivery.show_not_you(u.endpoint_id)
                if started_early:
                    self._handle(endpoint, DialogEvent.SPEECH_DROPPED)
                return
            if not started_early:
                await self._speech_started(endpoint)
        event = DialogEvent.UTTERANCE_HEARD if u.text else DialogEvent.NAME_ONLY_HEARD
        self._handle(endpoint, event)
        if u.text:
            latency = LatencyMarks(self._clock, vad_end=u.ended_at_monotonic)
            if u.recognized_at_monotonic is None:
                latency.mark(LatencyMark.ASR_FINAL)
            else:
                latency.mark_at(LatencyMark.ASR_FINAL, u.recognized_at_monotonic)
            await self._start_turn(
                endpoint, u.text, u.speaker, latency, heard_wake_word=u.heard_wake_word
            )
        elif self._name_only_reply is not None:
            await self._play_name_only_reply(endpoint, self._name_only_reply)

    async def on_speech_dropped(self, endpoint_id: str) -> None:
        endpoint = self._endpoints[endpoint_id]
        if endpoint.ignoring_this_utterance:
            return
        if endpoint.owner_only:
            if not endpoint.heard_owner_early:
                return
            endpoint.heard_owner_early = False
        self._handle(endpoint, DialogEvent.SPEECH_DROPPED)

    def _asks_to_stop(self, u: Utterance) -> bool:
        route = self._router.route(u.text, self._route_context(), heard_wake_word=u.heard_wake_word)
        return isinstance(route, RuleHit) and route.intent.ends_conversation

    def _exclusive_endpoint_other_than(self, endpoint: _Endpoint) -> str | None:
        for other in self._endpoints.values():
            if other.exclusive and other is not endpoint:
                return other.endpoint_id
        return None

    async def _start_turn(
        self,
        endpoint: _Endpoint,
        utterance: str,
        speaker: Speaker,
        latency: LatencyMarks,
        *,
        heard_wake_word: bool,
    ) -> None:
        await self._cancel_foreground(endpoint)
        heard = _HeardLine(
            utterance,
            speaker,
            self._clock.now(),
            spoken=True,
            endpoint_id=endpoint.endpoint_id,
            heard_wake_word=heard_wake_word,
        )
        task = self._running_task_group().create_task(
            self._run_turn(endpoint, heard, latency), name=f"turn-{endpoint.endpoint_id}"
        )
        self._turn = _ForegroundTurn(endpoint.endpoint_id, endpoint, task)

    async def on_text(self, endpoint_id: str, text: str) -> None:
        await self._cancel_foreground(None)
        heard = _HeardLine(
            text, Speaker.OWNER, self._clock.now(), spoken=False, endpoint_id=endpoint_id
        )
        task = self._running_task_group().create_task(
            self._run_text_turn(endpoint_id, heard), name=f"text-turn-{endpoint_id}"
        )
        self._turn = _ForegroundTurn(endpoint_id, None, task)

    def on_confirmation(self, endpoint_id: str, confirmation: Confirmation) -> None:
        if not confirmation.approved:
            self._running_task_group().create_task(
                self._report_card_rejected(report_to=endpoint_id, ticket_id=confirmation.ticket_id),
                name="card-rejected-report",
            )
            return
        self._running_task_group().create_task(
            self._carry_out_confirmed(confirmation, report_to=endpoint_id, by_voice=False),
            name="card-confirmed-call",
        )

    async def _turn_input(self, heard: _HeardLine) -> TurnInput:
        started_at = heard.started_at
        location = (
            await self._situation.for_cloud(started_at) if heard.speaker is Speaker.OWNER else None
        )
        muted = await self._mute_state.is_muted(started_at.date())
        return TurnInput(
            heard.utterance,
            history=self._working_memory.history(started_at, heard.speaker),
            situation=situation_summary(started_at, location=location, muted=muted),
            spoken=heard.spoken,
            speaker=heard.speaker,
            origin=Origin.USER,
            precise_location=self._location_for_tools(heard),
        )

    def _location_for_tools(self, heard: _HeardLine) -> GeoFix | None:
        if heard.speaker is Speaker.OWNER:
            return self._situation.precise(heard.started_at)
        return GeoFix(
            lat=self._home_latitude,
            lng=self._home_longitude,
            accuracy_m=_HOME_FIX_ACCURACY_M,
            at=heard.started_at,
            moving=False,
        )

    async def _play_name_only_reply(self, endpoint: _Endpoint, reply: PreparedSpeech) -> None:
        await self._cancel_foreground(endpoint)
        task = self._running_task_group().create_task(
            self._delivery.play_prepared(endpoint.endpoint_id, reply),
            name=f"name-only-reply-{endpoint.endpoint_id}",
        )
        self._turn = _ForegroundTurn(endpoint.endpoint_id, endpoint, task)

    async def _cancel_foreground(self, endpoint: _Endpoint | None) -> None:
        while self._turn is not None and not self._turn.task.done():
            old_turn = self._turn
            old_voice = old_turn.voice_endpoint
            answering = (DialogState.THINKING, DialogState.SPEAKING)
            if (
                old_voice is not None
                and old_voice is not endpoint
                and old_voice.dialog.state in answering
            ):
                self._handle(old_voice, DialogEvent.TURN_FINISHED)
            await self._cancel_turn(old_turn)

    async def _cancel_turn(self, turn: _ForegroundTurn) -> None:
        turn.task.cancel()
        if turn.voice_endpoint is not None:
            await self._delivery.stop_now(turn.endpoint_id)
        await asyncio.wait([turn.task])

    async def _run_turn(
        self, endpoint: _Endpoint, heard: _HeardLine, latency: LatencyMarks
    ) -> None:
        trace_id = self._ids.new("trace")
        spoken_reply = SpokenReply()
        from_tools: list[str] = []
        route_taken = _RouteTaken()
        added_speech = _AddedSpeech()
        interrupted = False
        with trace(trace_id), recording_latency(latency):
            try:
                async with (
                    aclosing(self._reply_sentences(heard, from_tools, route_taken)) as sentences,
                    aclosing(self._with_progress(endpoint, sentences, added_speech)) as spoken,
                ):
                    await self._delivery.reply(endpoint.endpoint_id, spoken, spoken_reply)
                self._handle(endpoint, DialogEvent.TURN_FINISHED)
                if route_taken.ends_conversation:
                    endpoint.asleep = True
                    self._handle(endpoint, DialogEvent.CONVERSATION_ENDED)
            except asyncio.CancelledError:
                interrupted = True
                raise
            finally:
                self._remember_turn(
                    heard,
                    trace_id,
                    spoken_reply,
                    interrupted,
                    from_tools,
                    route_taken,
                    not_reply=added_speech.positions,
                )
                self._publish_turn_completed(endpoint, latency, route_taken.kind, heard.speaker)

    async def _run_text_turn(self, endpoint_id: str, heard: _HeardLine) -> None:
        trace_id = self._ids.new("trace")
        answered = SpokenReply()
        from_tools: list[str] = []
        route_taken = _RouteTaken()
        interrupted = False
        endpoint_online = True
        with trace(trace_id):
            try:
                async with aclosing(
                    self._reply_sentences(heard, from_tools, route_taken)
                ) as replies:
                    async for reply in replies:
                        if not isinstance(reply, str):
                            continue
                        if endpoint_online:
                            endpoint_online = await self._delivery.send_text(
                                endpoint_id, reply, card=route_taken.card
                            )
                            if not endpoint_online:
                                logger.info(
                                    "端点 %s 不在线，这一轮的文字回复不再发，答完照样记下",
                                    endpoint_id,
                                )
                        answered.finished.append(reply)
                if endpoint_online and answered.finished:
                    self._delivery.end_text(endpoint_id)
            except asyncio.CancelledError:
                interrupted = True
                raise
            finally:
                self._remember_turn(heard, trace_id, answered, interrupted, from_tools, route_taken)
            if not endpoint_online and answered.finished:
                await self._delivery.remind_reply_not_delivered("".join(answered.finished))

    def _remember_turn(
        self,
        heard: _HeardLine,
        trace_id: str,
        spoken_reply: SpokenReply,
        interrupted: bool,
        from_tools: list[str],
        route_taken: _RouteTaken,
        *,
        not_reply: Collection[int] = (),
    ) -> None:
        finished, cut_off = spoken_reply.finished, spoken_reply.cut_off
        if not_reply:
            if cut_off is not None and len(finished) in not_reply:
                cut_off = None
            finished = [
                sentence for index, sentence in enumerate(finished) if index not in not_reply
            ]
        reply_text = reply_to_remember(
            finished, cut_off, interrupted=interrupted, from_tools=from_tools
        )
        if reply_text is None:
            return
        speaker = heard.speaker
        started_at = heard.started_at
        utterance = WorkingMessage(started_at, trace_id, Role.USER, speaker, heard.utterance)
        reply = WorkingMessage(
            started_at,
            trace_id,
            Role.ASSISTANT,
            speaker,
            reply_text,
            tool_call=route_taken.confirm_call,
        )
        self._working_memory.add_turn(utterance, reply)
        if self._task_group is None:
            logger.info("停机中，这一轮没写进工作记忆和对话记录的库")
            return
        self._task_group.create_task(self._save_turn([utterance, reply]), name="save-turn")
        if speaker == Speaker.OWNER:
            via = Via.VOICE if heard.spoken else Via.TYPED
            turn = ChatTurn(
                started_at, trace_id, heard.utterance, reply_text, via, card=route_taken.card
            )
            self._task_group.create_task(self._save_to_history(turn), name="save-history")
        self._bus.publish(
            TurnRecorded(
                ts=self._clock.now(),
                speaker=speaker,
                utterance=heard.utterance,
                reply=reply_text,
                spoken=heard.spoken,
                route=route_taken.kind,
                tools_called=tuple(route_taken.tools_called),
            )
        )

    def _remember_delivered(self) -> None:
        for delivered in self._delivery.take_delivered():
            self._remember_aside(
                delivered.at, self._ids.new("trace"), DELIVERED_NOTE, delivered.message.text
            )
            self._just_delivered = delivered.message

    def _remember_aside(self, at: datetime, trace_id: str, note: str, said: str) -> None:
        aside = WorkingMessage(at, trace_id, Role.USER, Speaker.OWNER, note)
        reply = WorkingMessage(at, trace_id, Role.ASSISTANT, Speaker.OWNER, said)
        self._working_memory.add_turn(aside, reply)
        if self._task_group is not None:
            self._task_group.create_task(self._save_turn([aside, reply]), name="save-aside")

    async def _save_turn(self, messages: list[WorkingMessage]) -> None:
        try:
            await self._store.append(messages, expired_before=self._clock.now() - WINDOW)
        except Exception:
            logger.exception("工作记忆没写进 ses_message")

    async def _save_to_history(self, turn: ChatTurn) -> None:
        try:
            await self._history.append(turn)
        except Exception:
            logger.exception("这一轮没写进对话记录（ses_history）")

    def _publish_turn_completed(
        self,
        endpoint: _Endpoint,
        latency: LatencyMarks,
        route: RouteKind | None,
        speaker: Speaker,
    ) -> None:
        self._bus.publish(
            TurnCompleted(
                ts=self._clock.now(),
                endpoint_id=endpoint.endpoint_id,
                speaker=speaker,
                asr_final_ms=latency.ms_since_vad_end(LatencyMark.ASR_FINAL),
                route_done_ms=latency.ms_since_vad_end(LatencyMark.ROUTE_DONE),
                llm_first_token_ms=latency.ms_since_vad_end(LatencyMark.LLM_FIRST_TOKEN),
                tts_first_chunk_ms=latency.ms_since_vad_end(LatencyMark.TTS_FIRST_CHUNK),
                play_start_ms=latency.ms_since_vad_end(LatencyMark.PLAY_START),
                route=route,
            )
        )

    async def _reply_sentences(
        self, heard: _HeardLine, from_tools: list[str], route_taken: _RouteTaken
    ) -> AsyncGenerator[str | ToolStarted | ToolFinished]:
        self._remember_delivered()
        route = self._router.route(
            heard.utterance, self._route_context(), heard_wake_word=heard.heard_wake_word
        )
        just_delivered = self._just_delivered
        if heard.speaker is Speaker.OWNER:
            self._just_delivered = None
        mark_latency(LatencyMark.ROUTE_DONE)
        route_taken.kind = _route_kind(route)
        answered_any = False
        try:
            match route:
                case RuleHit(intent=intent):
                    route_taken.ends_conversation = intent.ends_conversation
                    reply = await self._rule_reply(heard, intent, route_taken)
                    if reply:
                        yield reply
                case CloudTurn():
                    turn_input = await self._turn_input(heard)
                    async with aclosing(self._brain.respond(turn_input)) as replies:
                        async for event in replies:
                            if isinstance(event, Sentence):
                                answered_any = True
                                yield event.text
                            elif isinstance(event, ToolStarted | ToolFinished):
                                if isinstance(event, ToolStarted):
                                    route_taken.tools_called.append(event.name)
                                    if event.name == END_CONVERSATION_TOOL:
                                        route_taken.ends_conversation = True
                                yield event
                            elif isinstance(event, Remember):
                                from_tools.append(event.text)
                            elif isinstance(event, NeedConfirm):
                                route_taken.confirm_call = event.call
                                yield await self._ask_for_confirmation(event.pending, route_taken)
                case Defer():
                    yield await self._defer(heard)
                case Control(kind=ControlKind.UNDO):
                    yield await self._undo(heard)
                case Control(kind=ControlKind.CONFIRM):
                    yield await self._confirm(heard)
                case Control(kind=ControlKind.CANCEL):
                    self._awaiting = None
                    yield CONFIRM_CANCELLED_REPLY
                case Control(kind=ControlKind.RECALL):
                    yield self._recall()
                case Control(kind=ControlKind.USELESS):
                    yield await self._useless(heard, just_delivered)
                case Control(kind=kind):
                    raise AssertionError(f"控制词 {kind} 没接上")
                case _:
                    assert_never(route)
        except LlmError as error:
            logger.warning("模型出错，这一回合没答完：%s", error)
            if not answered_any and isinstance(
                self._router.route(
                    heard.utterance, self._route_context(), heard_wake_word=heard.heard_wake_word
                ),
                Defer,
            ):
                route_taken.kind = RouteKind.DEFER
                yield await self._defer(heard)
            else:
                yield MODEL_FAILED_REPLY
        except AuditWriteFailed as error:
            logger.exception("审计写不进去，这一回合停下")
            yield AUDIT_FAILED_AFTER_RUNNING_REPLY if error.after_running else AUDIT_FAILED_REPLY

    def _route_context(self) -> RouteContext:
        return RouteContext(
            awaiting_confirmation=self._awaiting_confirmation() is not None,
            after_proactive_message=self._just_delivered is not None,
        )

    async def _useless(self, heard: _HeardLine, just_delivered: Outbound | None) -> str:
        if just_delivered is None:
            raise AssertionError('没有刚送到的主动消息，路由不该回"这条没用"')
        if heard.speaker is not Speaker.OWNER:
            return USELESS_NOT_OWNER_REPLY
        if just_delivered.asked_for:
            return USELESS_ASKED_FOR_REPLY
        try:
            feedback = await self._feedback.mark_useless(just_delivered.source)
        except Exception:
            logger.exception('"这条没用"没记下来')
            return USELESS_NOT_SAVED_REPLY
        match feedback:
            case Feedback.LOWERED:
                return USELESS_LOWERED_REPLY
            case Feedback.STOPPED:
                return USELESS_STOPPED_REPLY
            case _:
                assert_never(feedback)

    def _recall(self) -> str:
        if not self._executor.recall_pending():
            return NOTHING_TO_RECALL_REPLY
        return RECALLED_REPLY

    async def _ask_for_confirmation(self, pending: Pending, route_taken: _RouteTaken) -> str:
        if pending.ticket.permission is not Permission.L3B:
            self._awaiting = pending
            return pending.confirm_text
        sent = await self._present_confirmation(pending)
        if not sent:
            return CARD_NOT_SENT_REPLY
        ticket = pending.ticket
        route_taken.card = CardPrompt(ticket.id, pending.confirm_text, ticket.expires_at)
        return CARD_SENT_REPLY

    async def _present_confirmation(self, pending: Pending) -> bool:
        ticket = pending.ticket
        delivered = await self._delivery.present_confirmation(
            ticket_id=ticket.id,
            title=pending.confirm_text,
            lines=pending.details,
            args_hash=ticket.args_hash,
            expires_at=ticket.expires_at,
        )
        return delivered is not CardDelivery.NOT_SENT

    def _awaiting_confirmation(self) -> Pending | None:
        if self._awaiting is not None and self._clock.now() >= self._awaiting.ticket.expires_at:
            self._awaiting = None
        return self._awaiting

    async def _confirm(self, heard: _HeardLine) -> str:
        pending = self._awaiting_confirmation()
        if pending is None:
            raise AssertionError("没有等着确认的单，路由不该回 CONFIRM")
        if heard.speaker is not Speaker.OWNER:
            sent = await self._present_confirmation(pending)
            if not sent:
                return OTHER_CONFIRM_CARD_NOT_SENT_REPLY
            if self._awaiting is pending:
                self._awaiting = None
            return OTHER_CONFIRM_CARD_SENT_REPLY
        self._awaiting = None
        confirmation = Confirmation(pending.ticket.id, ConfirmVia.VOICE, speaker=heard.speaker)
        self._running_task_group().create_task(
            self._carry_out_confirmed(
                confirmation, report_to=heard.endpoint_id, by_voice=heard.spoken
            ),
            name="confirmed-call",
        )
        return CONFIRMED_REPLY

    async def _carry_out_confirmed(
        self, confirmation: Confirmation, *, report_to: str, by_voice: bool
    ) -> None:
        started_at = self._clock.now()
        try:
            outcome = await self._executor.execute_confirmed(confirmation)
        except AuditWriteFailed:
            logger.exception("审计写不进去，确认的事办了，但没记下来")
            report = AUDIT_FAILED_AFTER_RUNNING_REPLY
            redeemed = True
        else:
            match outcome:
                case Recalled():
                    return
                case Done():
                    report = outcome.done_reply or CONFIRMED_DONE_REPLY
                case Denied() | Failed():
                    report = _not_done_reply(outcome)
                case _:
                    assert_never(outcome)
            redeemed = not isinstance(outcome, Denied)
        if confirmation.via is ConfirmVia.CARD and redeemed:
            await self._answer_card(confirmation.ticket_id, CardState.APPROVED)
        forgotten = [topic for topic, at in self._recently_forgotten if at >= started_at]
        self._record_report(
            report,
            by_voice=by_voice,
            note=CONFIRMED_OUTCOME_NOTE,
            remember=not any(mentions_topic(topic, report) for topic in forgotten),
        )
        if by_voice:
            await self._delivery.say_when_quiet(report_to, report, push_title=CONFIRMED_PUSH_TITLE)
        elif not await self._delivery.send_text(report_to, report):
            await self._delivery.remind_reply_not_delivered(report)

    async def _answer_card(self, ticket_id: str, answer: CardState) -> None:
        try:
            await self._history.answer_card(ticket_id, answer)
        except Exception:
            logger.exception("卡片批没批没记进对话记录（ses_history）")

    async def _report_card_rejected(self, *, report_to: str, ticket_id: str) -> None:
        await self._answer_card(ticket_id, CardState.REJECTED)
        self._record_report(CARD_REJECTED_REPLY, by_voice=False, note=CARD_REJECTED_NOTE)
        await self._delivery.send_text(report_to, CARD_REJECTED_REPLY)

    def _record_report(
        self, report: str, *, by_voice: bool, note: str, remember: bool = True
    ) -> None:
        now = self._clock.now()
        trace_id = self._ids.new("trace")
        if remember:
            self._remember_aside(now, trace_id, note, report)
        if self._task_group is None:
            return
        via = Via.VOICE if by_voice else Via.TYPED
        turn = ChatTurn(now, trace_id, "", report, via)
        self._task_group.create_task(self._save_to_history(turn), name="save-history")

    async def _undo(self, heard: _HeardLine) -> str:
        ctx = ToolContext(
            speaker=heard.speaker,
            origin=Origin.RULE,
            precise_location=self._location_for_tools(heard),
        )
        outcome = await self._executor.undo_last(ctx)
        if isinstance(outcome, Done):
            return UNDO_DONE_REPLY
        return _not_done_reply(outcome)

    async def _defer(self, heard: _HeardLine) -> str:
        if heard.speaker is not Speaker.OWNER:
            return DEFER_NOT_OWNER_REPLY
        try:
            await self._deferred.defer(heard.utterance, heard.started_at)
        except Exception:
            logger.exception("先记下来的话没写进 sch_job")
            return DEFER_FAILED_REPLY
        return DEFER_REPLY

    async def _rule_reply(
        self, heard: _HeardLine, intent: IntentMatch, route_taken: _RouteTaken
    ) -> str:
        if intent.tool is None:
            return intent.render_reply(builtin_fields(heard.started_at))
        call = ToolCall(id=self._ids.new("call"), name=intent.tool, args=intent.args)
        ctx = ToolContext(
            speaker=heard.speaker,
            origin=Origin.RULE,
            precise_location=self._location_for_tools(heard),
        )
        outcome = await self._executor.execute(call, ctx)
        match outcome:
            case Done(result=result):
                try:
                    return intent.render_reply(result.fields)
                except KeyError as missing:
                    logger.error(
                        "意图 %s 的回复模板要 %s，工具 %s 的结果里没有",
                        intent.intent_id,
                        missing,
                        intent.tool,
                    )
                    return RULE_TEMPLATE_BROKEN_REPLY
            case Denied() | Failed():
                return _not_done_reply(outcome)
            case Pending():
                return await self._ask_for_confirmation(outcome, route_taken)
            case _:
                assert_never(outcome)

    async def _with_progress(
        self,
        endpoint: _Endpoint,
        items: AsyncGenerator[str | ToolStarted | ToolFinished],
        added_speech: _AddedSpeech,
    ) -> AsyncGenerator[str]:
        queue: asyncio.Queue[str | ToolStarted | ToolFinished | _EndOfReply] = asyncio.Queue()

        async def read_replies() -> None:
            async for item in items:
                queue.put_nowait(item)
            queue.put_nowait(_END_OF_REPLY)

        reader = asyncio.ensure_future(read_replies())
        delivered = 0
        spoke = False
        said_progress = False
        tools_running = 0
        quiet_since = self._clock.monotonic()
        try:
            while True:
                item = await self._next_item(
                    queue,
                    self._seconds_until_something_to_say(
                        spoke=spoke,
                        waiting_for_tool=tools_running > 0 and not said_progress,
                        quiet_since=quiet_since,
                    ),
                )
                if isinstance(item, _EndOfReply):
                    break
                if isinstance(item, ToolFinished):
                    tools_running = max(0, tools_running - 1)
                    continue
                is_added = True
                if isinstance(item, ToolStarted):
                    tools_running += 1
                    if item.announce is None or spoke:
                        continue
                    text = item.announce
                elif isinstance(item, _SilenceTooLong):
                    text = PROGRESS_REPLY if spoke else FILLER_REPLY
                    said_progress = said_progress or spoke
                else:
                    text, is_added = item, False
                if not spoke:
                    self._handle(endpoint, DialogEvent.REPLY_STARTED)
                    spoke = True
                if is_added:
                    added_speech.positions.add(delivered)
                delivered += 1
                yield text
                quiet_since = self._clock.monotonic()
            await reader
        finally:
            reader.cancel()
            await asyncio.wait([reader])

    async def _next_item(
        self,
        queue: asyncio.Queue[str | ToolStarted | ToolFinished | _EndOfReply],
        wait_for: float | None,
    ) -> str | ToolStarted | ToolFinished | _EndOfReply | _SilenceTooLong:
        if not queue.empty():
            return queue.get_nowait()
        if wait_for is None:
            return await queue.get()
        try:
            async with self._clock.timeout(wait_for):
                return await queue.get()
        except TimeoutError:
            return _SILENCE_TOO_LONG

    def _seconds_until_something_to_say(
        self, *, spoke: bool, waiting_for_tool: bool, quiet_since: float
    ) -> float | None:
        if not spoke:
            allowed = FILLER_AFTER_SECONDS
        elif waiting_for_tool:
            allowed = PROGRESS_AFTER_SECONDS
        else:
            return None
        return allowed - (self._clock.monotonic() - quiet_since)

    def _handle(self, endpoint: _Endpoint, event: DialogEvent) -> None:
        was_open = endpoint.dialog.state == DialogState.OPEN
        endpoint.dialog.handle(event)
        new_state = endpoint.dialog.state

        if new_state != DialogState.IDLE:
            endpoint.asleep = False
        endpoint.wake_control.set_wake_required(
            new_state == DialogState.IDLE and _needs_name(endpoint)
        )

        if was_open and endpoint.window_timer is not None:
            endpoint.window_timer.cancel()
            endpoint.window_timer = None
        if new_state == DialogState.OPEN:
            endpoint.window_timer = self._running_task_group().create_task(
                self._close_window_later(endpoint), name=f"open-window-{endpoint.endpoint_id}"
            )
        self._delivery.on_state(endpoint.endpoint_id, new_state)

    async def _close_window_later(self, endpoint: _Endpoint) -> None:
        await self._clock.sleep(OPEN_WINDOW_SECONDS)
        endpoint.window_timer = None
        self._handle(endpoint, DialogEvent.WINDOW_EXPIRED)

    def _running_task_group(self) -> asyncio.TaskGroup:
        if self._task_group is None:
            raise RuntimeError("Session.run() 还没跑起来：开放窗口的计时、回合都要挂在它下面")
        return self._task_group


def _needs_name(endpoint: _Endpoint) -> bool:
    return not endpoint.name_only_after_stop or endpoint.asleep
