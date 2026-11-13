import asyncio
import logging
from collections import deque
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Sequence
from contextlib import aclosing
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

from argus.channels.audio import AudioChunk
from argus.channels.endpoint import ConfirmCard, Endpoint, EndpointOffline
from argus.channels.push import PushError, PushLevel, PushOutlet
from argus.channels.registry import EndpointRegistry
from argus.delivery.announcer import Announcer
from argus.delivery.exit_choice import Exit, choose_exit
from argus.delivery.outbound import PUSH_TITLE, Delivered, Outbound, Verdict
from argus.kernel.clock import Clock
from argus.kernel.dialog import DialogState
from argus.kernel.events import EventBus, ProactiveDelivered
from argus.kernel.tools import CardDelivery, CardPrompt
from argus.kernel.tracing import LatencyMark, mark_latency
from argus.speech.inference import TtsError

logger = logging.getLogger(__name__)

_BOUNDARY_TOLERANCE_MS = 1.0

_UNDELIVERED_REPLY_TITLE = "阿格斯回复了"
_REPLY_PREVIEW_CHARS = 100
CHAT_PAGE_URL = "argus://chat"
_DIGEST_TITLE = "简报"
_CARD_PUSH_TITLE = "待确认"
_ANSWERED_CARD_MEMORY = timedelta(days=1)
_CARD_PUSH_BODY = "有一项操作待确认"

_QUIET_STATES = frozenset({DialogState.IDLE, DialogState.OPEN})


_MAX_DELIVERED = 20


@dataclass
class SpokenReply:
    finished: list[str] = field(default_factory=list)
    cut_off: str | None = None


@dataclass(frozen=True)
class PreparedSpeech:
    sentence: str
    chunks: tuple[AudioChunk, ...]


@dataclass
class _HandedOver:
    sentence: str
    audio_ms: float = 0.0
    inserted: bool = False


class SentenceSynthesizer(Protocol):
    def synthesize(self, sentence: str) -> AsyncGenerator[AudioChunk]: ...


class Delivery:
    def __init__(
        self,
        synthesizer: SentenceSynthesizer,
        endpoints: EndpointRegistry,
        *,
        clock: Clock,
        push: PushOutlet | None = None,
        speaker_endpoint_id: str | None = None,
        app_endpoint_id: str | None = None,
        at_home: Callable[[], bool] | None = None,
        others_present: Callable[[], bool] | None = None,
        cards_approvable: Callable[[], bool] | None = None,
        bus: EventBus | None = None,
    ) -> None:
        self._synthesizer = synthesizer
        self._endpoints = endpoints
        self._clock = clock
        self._push = push
        self._speaker_endpoint_id = speaker_endpoint_id
        self._app_endpoint_id = app_endpoint_id
        self._at_home = at_home
        self._others_present = others_present
        self._cards_approvable = cards_approvable
        self._bus = bus
        self._pushed_cards: dict[str, ConfirmCard] = {}
        self._answered_cards: dict[str, datetime] = {}
        self._in_session: set[str] = set()
        self._announcers: dict[str, Announcer] = {}
        self._announcer_changed: dict[str, asyncio.Event] = {}
        self._announce_loops: set[str] = set()
        self._task_group: asyncio.TaskGroup | None = None
        self._say_locks: dict[str, asyncio.Lock] = {}
        self._quiet: dict[str, asyncio.Event] = {}
        self._saying_when_quiet: dict[str, asyncio.Event] = {}
        self._delivered: deque[Delivered] = deque(maxlen=_MAX_DELIVERED)

    async def reply(
        self, endpoint_id: str, sentences: AsyncIterator[str], spoken: SpokenReply
    ) -> None:
        endpoint = self._online_endpoint(endpoint_id)
        await self.stop_saying_when_quiet(endpoint_id)
        handed_over: list[_HandedOver] = []
        try:
            async with aclosing(
                self._synthesize_each(endpoint_id, sentences, handed_over)
            ) as chunks:
                await endpoint.play(chunks)
        finally:
            _fill_spoken(spoken, handed_over, endpoint.played_ms())

    def on_state(self, endpoint_id: str, state: DialogState) -> None:
        self._in_session.add(endpoint_id)
        quiet = self._quiet_event(endpoint_id)
        if state in _QUIET_STATES:
            quiet.set()
        else:
            quiet.clear()
        self._announcer(endpoint_id).on_state(state, self._clock.now())
        self._announcer_changed_event(endpoint_id).set()
        self._online_endpoint(endpoint_id).show_state(state)

    def show_not_you(self, endpoint_id: str) -> None:
        self._online_endpoint(endpoint_id).show_not_you()

    def on_endpoint_removed(self, endpoint_id: str) -> None:
        self._in_session.discard(endpoint_id)
        self._announcer(endpoint_id).on_state(DialogState.IDLE, self._clock.now())
        self._announcer_changed_event(endpoint_id).set()
        self._quiet_event(endpoint_id).set()

    async def run(self) -> None:
        async with asyncio.TaskGroup() as task_group:
            self._task_group = task_group
            try:
                await asyncio.Future()
            finally:
                self._task_group = None

    async def announce(self, message: Outbound) -> None:
        app = self._app_endpoint_id
        speaker = self._speaker_endpoint_id
        exit_chosen = choose_exit(
            message.verdict,
            app_voice_online=app is not None and app in self._in_session and self._is_online(app),
            at_home=self._at_home is None or self._at_home(),
            speaker_online=speaker is not None and self._is_online(speaker),
            personal=message.personal,
            others_present=self._others_present is not None and self._others_present(),
        )
        if exit_chosen is Exit.APP and app is not None:
            self._enqueue(app, message)
        elif exit_chosen is Exit.SPEAKER and speaker is not None:
            self._enqueue(speaker, message)
        else:
            why = "判成了通知" if message.verdict is Verdict.NOTIFY else "没有能念的出口"
            if await self._push_instead(
                _push_title(message), message.text, why=why, level=_push_level(message)
            ):
                self._record_delivered(message)

    def take_delivered(self) -> list[Delivered]:
        delivered = list(self._delivered)
        self._delivered.clear()
        return delivered

    def _record_delivered(self, message: Outbound) -> None:
        self._delivered.append(Delivered(self._clock.now(), message))
        if not message.on_phone_already:
            self._publish_delivered(message.text)

    def _publish_delivered(self, text: str) -> None:
        if self._bus is not None:
            self._bus.publish(ProactiveDelivered(ts=self._clock.now(), text=text))

    def _enqueue(self, endpoint_id: str, message: Outbound) -> None:
        self._announcer(endpoint_id).enqueue(message)
        self._announcer_changed_event(endpoint_id).set()
        if endpoint_id in self._announce_loops:
            return
        if self._task_group is None:
            raise RuntimeError("Delivery.run() 没在跑，主动消息没人念")
        self._announce_loops.add(endpoint_id)
        self._task_group.create_task(
            self._announce_loop(endpoint_id), name=f"announcer-{endpoint_id}"
        )

    async def stop_now(self, endpoint_id: str) -> None:
        await self._online_endpoint(endpoint_id).stop()

    async def stop_saying_when_quiet(self, endpoint_id: str) -> None:
        finished = self._saying_when_quiet.get(endpoint_id)
        if finished is None:
            return
        logger.info("端点 %s 先停掉正在念的到点消息", endpoint_id)
        await self._online_endpoint(endpoint_id).stop()
        await finished.wait()

    async def send_text(
        self, endpoint_id: str, text: str, *, card: CardPrompt | None = None
    ) -> bool:
        endpoint = self._endpoints.get(endpoint_id)
        if endpoint is None:
            return False
        try:
            await endpoint.send_text(text, card=card)
        except EndpointOffline:
            return False
        return True

    def end_text(self, endpoint_id: str) -> None:
        endpoint = self._endpoints.get(endpoint_id)
        if endpoint is not None:
            endpoint.end_text()

    async def remind_reply_not_delivered(self, reply: str) -> None:
        if self._push is None:
            logger.info("文字回复没送到，没配 Bark，不提醒")
            return
        if len(reply) > _REPLY_PREVIEW_CHARS:
            preview = reply[:_REPLY_PREVIEW_CHARS] + "…"
        else:
            preview = reply
        try:
            await self._push.push(
                _UNDELIVERED_REPLY_TITLE, preview, url=CHAT_PAGE_URL, level=PushLevel.NORMAL
            )
        except PushError as error:
            logger.warning("文字回复没送到，Bark 提醒也没推出去：%s", error)
            return
        logger.info("文字回复没送到，已经经 Bark 提醒")

    async def push_digest(self, text: str) -> bool:
        if self._push is None:
            logger.warning("没配 Bark，简报推不出去")
            return False
        try:
            await self._push.push(_DIGEST_TITLE, text, level=PushLevel.NORMAL)
        except PushError as error:
            logger.warning("简报没推出去：%s", error)
            return False
        return True

    async def push_text(self, title: str, text: str) -> bool:
        if self._push is None:
            logger.warning("没配 Bark，%s 推不出去", title)
            return False
        try:
            await self._push.push(title, text, level=PushLevel.NORMAL)
        except PushError as error:
            logger.warning("%s 没推出去：%s", title, error)
            return False
        self._publish_delivered(f"{title}\n{text}")
        return True

    async def present_confirmation(
        self,
        *,
        ticket_id: str,
        title: str,
        lines: Sequence[str],
        args_hash: str,
        expires_at: datetime,
    ) -> CardDelivery:
        if self._cards_approvable is not None and not self._cards_approvable():
            logger.info("App 还没配对，卡片批了也验不了签，没发")
            return CardDelivery.NOT_SENT
        card = ConfirmCard(
            ticket_id=ticket_id,
            title=title,
            lines=tuple(lines),
            args_hash=args_hash,
            expires_at=expires_at,
        )
        for endpoint in self._endpoints.online():
            if not endpoint.caps.confirm_card:
                continue
            try:
                await endpoint.show_card(card)
            except EndpointOffline:
                continue
            self._drop_expired_cards()
            self._pushed_cards[card.ticket_id] = card
            return CardDelivery.SHOWN
        return await self._push_card(card)

    async def _push_card(self, card: ConfirmCard) -> CardDelivery:
        if self._push is None:
            logger.info("App 没开着、也没配 Bark，卡片没发出去")
            return CardDelivery.NOT_SENT
        self._drop_expired_cards()
        self._pushed_cards[card.ticket_id] = card
        try:
            await self._push.push(
                _CARD_PUSH_TITLE,
                _CARD_PUSH_BODY,
                url=f"argus://confirm?id={card.ticket_id}",
                level=PushLevel.TIME_SENSITIVE,
            )
        except PushError as error:
            logger.warning("App 没开着，叫你确认的 Bark 没推出去：%s", error)
            self._pushed_cards.pop(card.ticket_id, None)
            return CardDelivery.NOT_SENT
        logger.info("App 没开着，推了 Bark 叫你打开确认单 %s", card.ticket_id)
        return CardDelivery.PUSHED

    def pushed_card(self, ticket_id: str) -> ConfirmCard | None:
        self._drop_expired_cards()
        return self._pushed_cards.get(ticket_id)

    def card_answered(self, ticket_id: str) -> None:
        if self._pushed_cards.pop(ticket_id, None) is not None:
            self._answered_cards[ticket_id] = self._clock.now()

    def was_card_answered(self, ticket_id: str) -> bool:
        self._drop_expired_cards()
        return ticket_id in self._answered_cards

    def _drop_expired_cards(self) -> None:
        now = self._clock.now()
        for ticket_id in [
            key for key, card in self._pushed_cards.items() if card.expires_at <= now
        ]:
            del self._pushed_cards[ticket_id]
        for ticket_id in [
            key
            for key, answered_at in self._answered_cards.items()
            if now - answered_at > _ANSWERED_CARD_MEMORY
        ]:
            del self._answered_cards[ticket_id]

    async def prepare(self, sentence: str) -> PreparedSpeech:
        async with aclosing(self._synthesizer.synthesize(sentence)) as sentence_chunks:
            chunks = tuple([chunk async for chunk in sentence_chunks])
        return PreparedSpeech(sentence, chunks)

    async def play_prepared(self, endpoint_id: str, speech: PreparedSpeech) -> None:
        endpoint = self._online_endpoint(endpoint_id)
        await self.stop_saying_when_quiet(endpoint_id)
        async with aclosing(_replay(speech.chunks)) as chunks:
            await endpoint.play(chunks)

    async def say_when_quiet(
        self,
        endpoint_id: str,
        text: str,
        *,
        push_title: str | None,
        push_level: PushLevel = PushLevel.TIME_SENSITIVE,
        push_if_cut_off: bool = False,
    ) -> bool:
        async with self._say_locks.setdefault(endpoint_id, asyncio.Lock()):
            endpoint = self._endpoints.get(endpoint_id)
            if endpoint is None or not endpoint.is_online():
                return await self._push_instead(
                    push_title, text, why="音箱不在线", level=push_level
                )
            try:
                speech = await self.prepare(text)
            except TtsError as error:
                logger.warning("到点的消息合成出错：%s", error)
                return await self._push_instead(push_title, text, why="合成出错", level=push_level)
            quiet = self._quiet_event(endpoint_id)
            while not quiet.is_set():
                await quiet.wait()
            if not endpoint.is_online():
                return await self._push_instead(
                    push_title, text, why="等的时候端点断了", level=push_level
                )
            finished = asyncio.Event()
            self._saying_when_quiet[endpoint_id] = finished
            try:
                async with aclosing(_replay(speech.chunks)) as chunks:
                    await endpoint.play(chunks)
            finally:
                del self._saying_when_quiet[endpoint_id]
                finished.set()
            if push_if_cut_off and _cut_off(speech, endpoint.played_ms()):
                await self._push_instead(push_title, text, why="没念完就停了", level=push_level)
            return True

    async def _announce_loop(self, endpoint_id: str) -> None:
        announcer = self._announcer(endpoint_id)
        changed = self._announcer_changed_event(endpoint_id)
        while True:
            changed.clear()
            now = self._clock.now()
            announcement = announcer.next_when_free(now)
            for expired in announcer.take_expired():
                if await self._push_instead(
                    _push_title(expired),
                    expired.text,
                    why="排队排过了时候",
                    level=_push_level(expired),
                ):
                    self._record_delivered(expired)
            if announcement is not None:
                if announcement.message.personal and self._others_can_hear(endpoint_id):
                    if await self._push_instead(
                        _push_title(announcement.message),
                        announcement.message.text,
                        why="家里有外人",
                        level=_push_level(announcement.message),
                    ):
                        self._record_delivered(announcement.message)
                elif await self.say_when_quiet(
                    endpoint_id,
                    announcement.text,
                    push_title=_push_title(announcement.message),
                    push_level=_push_level(announcement.message),
                    push_if_cut_off=announcement.message.asked_for,
                ):
                    self._record_delivered(announcement.message)
                announcer.on_announced(self._clock.now())
                continue
            wake_at = announcer.wake_at(now)
            await self._wait_for_change(
                changed, None if wake_at is None else (wake_at - now).total_seconds()
            )

    async def _wait_for_change(self, changed: asyncio.Event, seconds: float | None) -> None:
        if seconds is None:
            await changed.wait()
            return
        try:
            async with self._clock.timeout(seconds):
                await changed.wait()
        except TimeoutError:
            pass

    def _announcer(self, endpoint_id: str) -> Announcer:
        return self._announcers.setdefault(endpoint_id, Announcer())

    def _announcer_changed_event(self, endpoint_id: str) -> asyncio.Event:
        return self._announcer_changed.setdefault(endpoint_id, asyncio.Event())

    async def _push_instead(
        self,
        title: str | None,
        text: str,
        *,
        why: str,
        level: PushLevel = PushLevel.TIME_SENSITIVE,
    ) -> bool:
        if title is None:
            logger.info("到点的消息%s，内容已经另推到手机，不再推", why)
            return True
        if self._push is None:
            logger.warning("到点的消息%s，又没配 Bark，这条没能告诉你", why)
            return False
        try:
            await self._push.push(title, text, level=level)
        except PushError as error:
            logger.warning("到点的消息%s，Bark 也没推出去：%s", why, error)
            return False
        logger.info("到点的消息%s，改推了 Bark", why)
        return True

    def _others_can_hear(self, endpoint_id: str) -> bool:
        return (
            endpoint_id == self._speaker_endpoint_id
            and self._others_present is not None
            and self._others_present()
        )

    def _is_online(self, endpoint_id: str) -> bool:
        endpoint = self._endpoints.get(endpoint_id)
        return endpoint is not None and endpoint.is_online()

    def _quiet_event(self, endpoint_id: str) -> asyncio.Event:
        quiet = self._quiet.get(endpoint_id)
        if quiet is None:
            quiet = asyncio.Event()
            quiet.set()
            self._quiet[endpoint_id] = quiet
        return quiet

    def _online_endpoint(self, endpoint_id: str) -> Endpoint:
        endpoint = self._endpoints.get(endpoint_id)
        if endpoint is None:
            raise RuntimeError(f"端点 {endpoint_id} 不在线")
        return endpoint

    async def _synthesize_each(
        self, endpoint_id: str, sentences: AsyncIterator[str], handed_over: list[_HandedOver]
    ) -> AsyncGenerator[AudioChunk]:
        async for sentence in sentences:
            if handed_over:
                async with aclosing(self._insert_urgent(endpoint_id, handed_over)) as inserted:
                    async for chunk in inserted:
                        yield chunk
            try:
                async with aclosing(self._hand_over(sentence, handed_over)) as sentence_chunks:
                    async for chunk in sentence_chunks:
                        yield chunk
            except TtsError as error:
                logger.warning("合成出错，这次回复后面的不念了：%s", error)
                return

    async def _insert_urgent(
        self, endpoint_id: str, handed_over: list[_HandedOver]
    ) -> AsyncGenerator[AudioChunk]:
        if self._others_can_hear(endpoint_id):
            return
        announcement = self._announcer(endpoint_id).next_between_sentences(self._clock.now())
        if announcement is None:
            return
        reached_you = False
        try:
            async with aclosing(
                self._hand_over(announcement.text, handed_over, inserted=True)
            ) as chunks:
                async for chunk in chunks:
                    reached_you = True
                    yield chunk
        except TtsError as error:
            logger.warning("插播的消息合成出错：%s", error)
            if await self._push_instead(
                _push_title(announcement.message), announcement.message.text, why="合成出错"
            ):
                reached_you = True
        finally:
            if reached_you:
                self._record_delivered(announcement.message)

    async def _hand_over(
        self, sentence: str, handed_over: list[_HandedOver], *, inserted: bool = False
    ) -> AsyncGenerator[AudioChunk]:
        current: _HandedOver | None = None
        async with aclosing(self._synthesizer.synthesize(sentence)) as sentence_chunks:
            async for chunk in sentence_chunks:
                mark_latency(LatencyMark.TTS_FIRST_CHUNK)
                if current is None:
                    current = _HandedOver(sentence, inserted=inserted)
                    handed_over.append(current)
                current.audio_ms += chunk.duration_ms
                yield chunk


def _push_title(message: Outbound) -> str | None:
    return None if message.on_phone_already else PUSH_TITLE


def _push_level(message: Outbound) -> PushLevel:
    return PushLevel.TIME_SENSITIVE if message.urgent else PushLevel.NORMAL


def _cut_off(speech: PreparedSpeech, played_ms: float) -> bool:
    total_ms = sum(chunk.duration_ms for chunk in speech.chunks)
    return played_ms + _BOUNDARY_TOLERANCE_MS < total_ms


async def _replay(chunks: tuple[AudioChunk, ...]) -> AsyncGenerator[AudioChunk]:
    for chunk in chunks:
        yield chunk


def _fill_spoken(spoken: SpokenReply, handed_over: list[_HandedOver], played_ms: float) -> None:
    sentence_end_ms = 0.0
    for handed in handed_over:
        sentence_start_ms = sentence_end_ms
        sentence_end_ms += handed.audio_ms
        if sentence_end_ms <= played_ms + _BOUNDARY_TOLERANCE_MS:
            if not handed.inserted:
                spoken.finished.append(handed.sentence)
        elif sentence_start_ms < played_ms - _BOUNDARY_TOLERANCE_MS:
            if not handed.inserted:
                spoken.cut_off = handed.sentence
            return
        else:
            return
