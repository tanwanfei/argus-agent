import asyncio
import logging
from collections.abc import AsyncGenerator, AsyncIterator

from argus.channels.audio import FRAME_BYTES, FRAME_MS, AudioChunk, AudioFrame
from argus.channels.endpoint import Capabilities, ConfirmCard, EndpointOffline
from argus.infra.endpoints.app_ws.messages import (
    BargeIn,
    ConfirmationBlock,
    ConfirmCardMessage,
    DownText,
    History,
    NotYou,
    PlayBegin,
    PlayEnd,
    ShownState,
    State,
    TextEnd,
    encode_downstream,
)
from argus.infra.endpoints.app_ws.opus import OpusDecoder, OpusEncoder
from argus.kernel.clock import Clock
from argus.kernel.dialog import DialogState
from argus.kernel.tools import CardPrompt, Confirmation
from argus.kernel.tracing import LatencyMark, mark_latency

logger = logging.getLogger(__name__)

APP_ENDPOINT_ID = "app"

APP_CAPS = Capabilities(audio_in=True, audio_out=True, text=True, confirm_card=True, location=False)

_MAX_QUEUED_FRAMES = 2000 // FRAME_MS

_LEAD_S = 1.0

_SHOWN_ON_APP: dict[DialogState, ShownState] = {
    DialogState.IDLE: "idle",
    DialogState.LISTENING: "listening",
    DialogState.THINKING: "thinking",
    DialogState.SPEAKING: "speaking",
    DialogState.OPEN: "idle",
}


class _TwentyMsSlicer:
    def __init__(self) -> None:
        self._leftover = b""

    def slice(self, pcm: bytes) -> list[bytes]:
        pending = self._leftover + pcm
        whole_bytes = len(pending) - len(pending) % FRAME_BYTES
        self._leftover = pending[whole_bytes:]
        return [
            pending[start : start + FRAME_BYTES] for start in range(0, whole_bytes, FRAME_BYTES)
        ]

    def take_leftover(self) -> bytes:
        leftover = self._leftover
        self._leftover = b""
        return leftover


class AppEndpoint:
    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._decoder = OpusDecoder()
        self._encoder = OpusEncoder()
        self._upstream_slicer = _TwentyMsSlicer()
        self._queued_frames: asyncio.Queue[AudioFrame | None] = asyncio.Queue()
        self._closed = False
        self._audio_started = False
        self._audio_started_or_closed = asyncio.Event()
        self._dropped_frames = 0
        self._bad_packets = 0
        self._outgoing: asyncio.Queue[bytes | str | None] = asyncio.Queue()
        self._barge_ins: asyncio.Queue[BargeIn | None] = asyncio.Queue()
        self._texts: asyncio.Queue[str | None] = asyncio.Queue()
        self._confirmations: asyncio.Queue[Confirmation | None] = asyncio.Queue()
        self._sending: asyncio.Task[bool] | None = None
        self._segment_begun = False
        self._sent_ms = 0.0
        self._app_done_at = 0.0
        self._played_ms = 0.0
        self._last_shown_state: ShownState | None = None

    @property
    def id(self) -> str:
        return APP_ENDPOINT_ID

    @property
    def caps(self) -> Capabilities:
        return APP_CAPS

    def is_online(self) -> bool:
        return not self._closed

    def receive_packet(self, packet: bytes) -> None:
        if self._closed:
            return
        if not self._audio_started:
            self._audio_started = True
            self._audio_started_or_closed.set()
        try:
            pcm = self._decoder.decode(packet)
        except ValueError as error:
            self._bad_packets += 1
            if self._bad_packets == 1:
                logger.warning("App 发来的音频包解不开，丢掉（%d 字节）：%s", len(packet), error)
            return
        for frame_pcm in self._upstream_slicer.slice(pcm):
            self._queue_latest(AudioFrame(frame_pcm))

    def _queue_latest(self, frame: AudioFrame) -> None:
        if self._queued_frames.qsize() >= _MAX_QUEUED_FRAMES:
            self._queued_frames.get_nowait()
            self._dropped_frames += 1
            if self._dropped_frames == 1:
                logger.warning("App 的声音读得太慢，开始丢弃最旧的帧")
        self._queued_frames.put_nowait(frame)

    def receive_barge_in(self, message: BargeIn) -> None:
        if not self._closed:
            self._barge_ins.put_nowait(message)

    def receive_text(self, text: str) -> None:
        if not self._closed:
            self._texts.put_nowait(text)

    def receive_confirmation(self, confirmation: Confirmation) -> None:
        if not self._closed:
            self._confirmations.put_nowait(confirmation)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._audio_started_or_closed.set()
        self._queued_frames.put_nowait(None)
        self._outgoing.put_nowait(None)
        self._barge_ins.put_nowait(None)
        self._texts.put_nowait(None)
        self._confirmations.put_nowait(None)
        if self._dropped_frames:
            logger.warning(
                "这次连接读得慢，一共丢了 %d 帧（%d ms）",
                self._dropped_frames,
                self._dropped_frames * FRAME_MS,
            )
        if self._bad_packets:
            logger.warning("这次连接一共有 %d 个音频包解不开", self._bad_packets)

    async def wait_for_audio(self) -> bool:
        await self._audio_started_or_closed.wait()
        return self._audio_started

    async def frames(self) -> AsyncGenerator[AudioFrame]:
        while True:
            frame = await self._queued_frames.get()
            if frame is None:
                return
            yield frame

    async def outgoing(self) -> AsyncGenerator[bytes | str]:
        while True:
            frame = await self._outgoing.get()
            if frame is None:
                return
            yield frame

    async def barge_ins(self) -> AsyncGenerator[BargeIn]:
        while True:
            message = await self._barge_ins.get()
            if message is None:
                return
            yield message

    async def texts(self) -> AsyncGenerator[str]:
        while True:
            text = await self._texts.get()
            if text is None:
                return
            yield text

    async def confirmations(self) -> AsyncGenerator[Confirmation]:
        while True:
            confirmation = await self._confirmations.get()
            if confirmation is None:
                return
            yield confirmation

    async def play(self, chunks: AsyncIterator[AudioChunk]) -> None:
        if self._sending is not None:
            raise RuntimeError(f"端点 {APP_ENDPOINT_ID} 上一次 play() 还没结束")
        self._sent_ms = 0.0
        self._app_done_at = self._clock.monotonic()
        sending = asyncio.create_task(self._send_all(chunks), name=f"play-{APP_ENDPOINT_ID}")
        self._sending = sending
        played_through = False
        try:
            played_through = await sending
        except asyncio.CancelledError:
            caller = asyncio.current_task()
            if caller is not None and caller.cancelling() > 0:
                raise
        finally:
            self._sending = None
            if played_through:
                self._played_ms = self._sent_ms
            else:
                self._played_ms = self._estimate_played_ms()

    async def stop(self) -> None:
        sending = self._sending
        if sending is None:
            return
        sending.cancel()
        await asyncio.wait([sending])

    def played_ms(self) -> float:
        return self._played_ms

    async def _send_all(self, chunks: AsyncIterator[AudioChunk]) -> bool:
        slicer = _TwentyMsSlicer()
        try:
            async for chunk in chunks:
                for frame_pcm in slicer.slice(chunk.pcm):
                    await self._wait_for_app_to_catch_up()
                    if self._closed:
                        return False
                    self._queue_audio(frame_pcm)
                    self._sent_ms += FRAME_MS
            leftover = slicer.take_leftover()
            if leftover:
                await self._wait_for_app_to_catch_up()
                if self._closed:
                    return False
                self._queue_audio(leftover + bytes(FRAME_BYTES - len(leftover)))
                self._sent_ms += AudioChunk(leftover).duration_ms
        finally:
            if self._segment_begun:
                self._queue(encode_downstream(PlayEnd()))
                self._segment_begun = False
        app_still_playing_s = self._app_done_at - self._clock.monotonic()
        if app_still_playing_s > 0:
            await self._clock.sleep(app_still_playing_s)
        return True

    async def _wait_for_app_to_catch_up(self) -> None:
        ahead_s = self._app_done_at - self._clock.monotonic()
        if ahead_s > _LEAD_S:
            await self._clock.sleep(ahead_s - _LEAD_S)

    def _estimate_played_ms(self) -> float:
        still_buffered_ms = max(0.0, self._app_done_at - self._clock.monotonic()) * 1000
        return max(0.0, self._sent_ms - still_buffered_ms)

    def _queue_audio(self, frame_pcm: bytes) -> None:
        if not self._segment_begun:
            self._queue(encode_downstream(PlayBegin()))
            self._segment_begun = True
        self._queue(self._encoder.encode(frame_pcm))
        mark_latency(LatencyMark.PLAY_START)
        self._app_done_at = max(self._app_done_at, self._clock.monotonic()) + FRAME_MS / 1000

    def _queue(self, frame: bytes | str) -> None:
        if not self._closed:
            self._outgoing.put_nowait(frame)

    async def send_text(self, text: str, *, card: CardPrompt | None = None) -> None:
        if self._closed:
            raise EndpointOffline(f"端点 {APP_ENDPOINT_ID} 的连接断了")
        confirmation = None
        if card is not None:
            confirmation = ConfirmationBlock(
                ticket_id=card.ticket_id,
                title=card.title,
                expires_at=card.expires_at,
                state="pending",
            )
        self._queue(encode_downstream(DownText(text=text, confirmation=confirmation)))

    def send_history(self, page: History) -> None:
        self._queue(encode_downstream(page))

    async def show_card(self, card: ConfirmCard) -> None:
        if self._closed:
            raise EndpointOffline(f"端点 {APP_ENDPOINT_ID} 的连接断了")
        message = ConfirmCardMessage(
            ticket_id=card.ticket_id,
            title=card.title,
            lines=card.lines,
            args_hash=card.args_hash,
            expires_at=card.expires_at,
        )
        self._queue(encode_downstream(message))

    def end_text(self) -> None:
        self._queue(encode_downstream(TextEnd()))

    def show_not_you(self) -> None:
        self._queue(encode_downstream(NotYou()))

    def show_state(self, state: DialogState) -> None:
        shown = _SHOWN_ON_APP[state]
        if shown == self._last_shown_state:
            return
        self._last_shown_state = shown
        self._queue(encode_downstream(State(state=shown)))
