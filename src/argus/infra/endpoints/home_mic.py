import asyncio
import logging
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import aclosing
from typing import Protocol

from argus.channels.audio import AudioChunk, AudioFrame
from argus.channels.endpoint import Capabilities, ConfirmCard
from argus.infra.audio.devices import AudioDeviceError
from argus.kernel.clock import Clock
from argus.kernel.dialog import DialogState
from argus.kernel.tools import CardPrompt

logger = logging.getLogger(__name__)

HOME_MIC_CAPS = Capabilities(
    audio_in=True, audio_out=True, text=False, confirm_card=False, location=False
)

_OFFLINE_AFTER_NO_FRAMES_S = 2.0
_RETRY_INTERVAL_S = 2.0


class Capture(Protocol):
    def frames(self) -> AsyncGenerator[AudioFrame]: ...


class Playback(Protocol):
    async def play(self, chunks: AsyncIterator[AudioChunk]) -> None: ...

    def played_ms(self) -> float: ...


class HomeMicEndpoint:
    def __init__(
        self,
        endpoint_id: str,
        capture: Capture,
        playback: Playback,
        clock: Clock,
        refresh_devices: Callable[[], None],
    ) -> None:
        self._id = endpoint_id
        self._capture = capture
        self._playback = playback
        self._clock = clock
        self._refresh_devices = refresh_devices
        self._last_frame_at: float | None = None
        self._playing: asyncio.Task[None] | None = None

    @property
    def id(self) -> str:
        return self._id

    @property
    def caps(self) -> Capabilities:
        return HOME_MIC_CAPS

    def is_online(self) -> bool:
        if self._last_frame_at is None:
            return False
        return self._clock.monotonic() - self._last_frame_at < _OFFLINE_AFTER_NO_FRAMES_S

    async def frames(self) -> AsyncGenerator[AudioFrame]:
        offline_since: float | None = None
        while True:
            try:
                async with aclosing(self._capture.frames()) as captured_frames:
                    while True:
                        frame = await self._next_frame(captured_frames)
                        if frame is None:
                            break
                        if offline_since is not None:
                            logger.info(
                                "端点 %s 掉线 %.0f 秒后重新接上",
                                self._id,
                                self._clock.monotonic() - offline_since,
                            )
                            offline_since = None
                        self._last_frame_at = self._clock.monotonic()
                        yield frame
                problem = f"{_OFFLINE_AFTER_NO_FRAMES_S:.0f} 秒没收到声音帧"
            except AudioDeviceError as error:
                if self._last_frame_at is None:
                    raise
                problem = str(error)
            if offline_since is None:
                offline_since = self._clock.monotonic()
                logger.warning(
                    "端点 %s 掉线：%s。每 %.0f 秒重试一次",
                    self._id,
                    problem,
                    _RETRY_INTERVAL_S,
                )
            await self._clock.sleep(_RETRY_INTERVAL_S)
            if self._playing is None:
                self._refresh_devices()

    async def _next_frame(self, captured_frames: AsyncIterator[AudioFrame]) -> AudioFrame | None:
        try:
            async with self._clock.timeout(_OFFLINE_AFTER_NO_FRAMES_S):
                return await anext(captured_frames)
        except TimeoutError:
            return None

    async def play(self, chunks: AsyncIterator[AudioChunk]) -> None:
        if self._playing is not None:
            raise RuntimeError(f"端点 {self._id} 上一次 play() 还没结束")
        playing = asyncio.create_task(self._playback.play(chunks), name=f"play-{self._id}")
        self._playing = playing
        try:
            await playing
        except asyncio.CancelledError:
            caller = asyncio.current_task()
            if caller is not None and caller.cancelling() > 0:
                raise
        except AudioDeviceError as error:
            logger.warning("端点 %s 放音出错，这句没放完：%s", self._id, error)
        finally:
            self._playing = None

    async def stop(self) -> None:
        playing = self._playing
        if playing is None:
            return
        playing.cancel()
        await asyncio.wait([playing])

    def played_ms(self) -> float:
        return self._playback.played_ms()

    async def send_text(self, text: str, *, card: CardPrompt | None = None) -> None:
        raise NotImplementedError(f"端点 {self._id} 是阵列加音箱，没有屏幕显示文字")

    async def show_card(self, card: ConfirmCard) -> None:
        raise NotImplementedError(f"端点 {self._id} 是阵列加音箱，弹不了确认卡片")

    def show_state(self, state: DialogState) -> None:
        pass

    def end_text(self) -> None:
        pass

    def show_not_you(self) -> None:
        pass
