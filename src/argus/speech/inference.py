import asyncio
import logging
import threading
from collections.abc import AsyncGenerator, Callable
from typing import Protocol

from argus.channels.audio import AudioChunk, AudioClip, AudioFrame
from argus.kernel.clock import Clock

logger = logging.getLogger(__name__)


class AsrSession(Protocol):
    def feed(self, frame: AudioFrame) -> None: ...

    async def finish(self, clip: AudioClip) -> str: ...

    def cancel(self) -> None: ...


class StreamingAsr(Protocol):
    def start(self, heard_so_far: AudioClip) -> AsrSession: ...


class Asr(Protocol):
    def transcribe(self, clip: AudioClip) -> str: ...


class AsrError(Exception):
    pass


class Tts(Protocol):
    def synthesize(self, sentence: str) -> AudioChunk: ...


class TtsError(Exception):
    pass


class Recognizer:
    def __init__(self, asr: Asr) -> None:
        self._asr = asr
        self._serial = SerialInference()

    async def transcribe(self, clip: AudioClip) -> str:
        return await self._serial.run(lambda: self._asr.transcribe(clip), skipped_result="")


class WholeClipAsr:
    def __init__(self, recognizer: Recognizer) -> None:
        self._recognizer = recognizer

    def start(self, heard_so_far: AudioClip) -> AsrSession:
        return _WholeClipSession(self._recognizer)


class _WholeClipSession:
    def __init__(self, recognizer: Recognizer) -> None:
        self._recognizer = recognizer

    def feed(self, frame: AudioFrame) -> None:
        pass

    async def finish(self, clip: AudioClip) -> str:
        return await self._recognizer.transcribe(clip)

    def cancel(self) -> None:
        pass


class FallbackAsr:
    def __init__(
        self,
        cloud: StreamingAsr,
        local: Recognizer,
        clock: Clock,
        *,
        wait_after_speech_end_seconds: float = 1.0,
    ) -> None:
        self._cloud = cloud
        self._local = local
        self._clock = clock
        self._wait_after_speech_end_seconds = wait_after_speech_end_seconds

    def start(self, heard_so_far: AudioClip) -> AsrSession:
        return _FallbackSession(
            self._cloud.start(heard_so_far),
            self._local,
            self._clock,
            self._wait_after_speech_end_seconds,
        )


class _FallbackSession:
    def __init__(
        self, cloud: AsrSession, local: Recognizer, clock: Clock, wait_seconds: float
    ) -> None:
        self._cloud = cloud
        self._local = local
        self._clock = clock
        self._wait_seconds = wait_seconds

    def feed(self, frame: AudioFrame) -> None:
        self._cloud.feed(frame)

    async def finish(self, clip: AudioClip) -> str:
        try:
            async with self._clock.timeout(self._wait_seconds):
                return await self._cloud.finish(clip)
        except AsrError as error:
            reason = str(error)
        except TimeoutError:
            reason = f"判停后 {self._wait_seconds:g} 秒还没拿到最终文字"
        self._cloud.cancel()
        logger.warning("云端这句没识别出来，改用本地：%s", reason)
        return await self._local.transcribe(clip)

    def cancel(self) -> None:
        self._cloud.cancel()


class Synthesizer:
    def __init__(self, tts: Tts) -> None:
        self._tts = tts
        self._serial = SerialInference()

    async def synthesize(self, sentence: str) -> AsyncGenerator[AudioChunk]:
        whole_sentence = await self._serial.run(
            lambda: self._tts.synthesize(sentence), skipped_result=AudioChunk(b"")
        )
        yield whole_sentence


class SerialInference:
    def __init__(self) -> None:
        self._lock = threading.Lock()

    async def run[ResultT](self, infer: Callable[[], ResultT], skipped_result: ResultT) -> ResultT:
        caller_gave_up = threading.Event()

        def run_in_thread() -> ResultT:
            with self._lock:
                if caller_gave_up.is_set():
                    return skipped_result
                return infer()

        try:
            return await asyncio.to_thread(run_in_thread)
        except asyncio.CancelledError:
            caller_gave_up.set()
            raise
