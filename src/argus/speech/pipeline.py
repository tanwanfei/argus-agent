import asyncio
import logging
import unicodedata
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from argus.channels.audio import FRAME_MS, AudioClip, AudioFrame
from argus.kernel.clock import Clock
from argus.kernel.tools import Speaker
from argus.speech.inference import AsrError, AsrSession, StreamingAsr
from argus.speech.speaker import SpeakerCheck
from argus.speech.vad import SpeechEnd, SpeechStart, Vad
from argus.speech.wakeword import WakeWord

logger = logging.getLogger(__name__)

_NAME_ONLY_MAX_CHARS = 3

_EARLY_CHECK_SECONDS = (1.0, 2.0)


@dataclass(frozen=True)
class Utterance:
    endpoint_id: str
    text: str
    ts: datetime
    speaker: Speaker
    ended_at_monotonic: float
    heard_wake_word: bool = False
    recognized_at_monotonic: float | None = None


class UtteranceHandler(Protocol):
    async def on_speech_start(self, endpoint_id: str) -> None: ...

    async def on_utterance(self, u: Utterance) -> None: ...

    async def on_speech_dropped(self, endpoint_id: str) -> None: ...

    async def on_owner_heard(self, endpoint_id: str) -> None: ...


class SpeechPipeline:
    def __init__(
        self,
        endpoint_id: str,
        wake_word: WakeWord | None,
        vad: Vad,
        asr: StreamingAsr,
        handler: UtteranceHandler,
        clock: Clock,
        speaker_check: SpeakerCheck | None = None,
        *,
        check_speaker_early: bool = False,
    ) -> None:
        self._endpoint_id = endpoint_id
        self._speaker_check = speaker_check
        self._early_speaker_check = speaker_check if check_speaker_early else None
        self._wake_word = wake_word
        self._vad = vad
        self._asr = asr
        self._handler = handler
        self._clock = clock
        self._wake_required = wake_word is not None
        self._in_utterance = False
        self._heard_wake_word = False
        self._addressed = False
        self._asr_session: AsrSession | None = None
        self._unaddressed_audio = bytearray()
        self._early_check_seconds: list[float] = []
        self._early_audio = bytearray()
        self._frames_since_start = 0

    def set_wake_required(self, required: bool) -> None:
        if self._wake_word is None:
            return
        self._wake_required = required

    async def run(self, frames: AsyncIterator[AudioFrame]) -> None:
        try:
            async for frame in frames:
                heard_wake_word = self._wake_word is not None and self._wake_word.feed(frame)
                event = self._vad.feed(frame)
                if isinstance(event, SpeechStart):
                    await self._on_speech_start(event.heard_so_far)
                elif self._in_utterance:
                    self._pass_on(frame)
                    if not isinstance(event, SpeechEnd):
                        await self._check_speaker_early(frame)
                if heard_wake_word:
                    await self._on_wake_word()
                if isinstance(event, SpeechEnd):
                    await self._on_speech_end(event.clip)
        finally:
            if self._asr_session is not None:
                self._asr_session.cancel()
                self._asr_session = None

    def _pass_on(self, frame: AudioFrame) -> None:
        if self._asr_session is not None:
            self._asr_session.feed(frame)
        else:
            self._unaddressed_audio += frame.pcm

    async def _on_speech_start(self, heard_so_far: AudioClip) -> None:
        self._in_utterance = True
        self._heard_wake_word = False
        self._addressed = not self._wake_required
        self._unaddressed_audio = bytearray(heard_so_far.pcm)
        if self._early_speaker_check is not None:
            self._early_check_seconds = list(_EARLY_CHECK_SECONDS)
            self._early_audio = bytearray(heard_so_far.pcm)
            self._frames_since_start = 0
        if self._addressed:
            await self._begin_addressed_utterance()

    async def _on_wake_word(self) -> None:
        if not self._in_utterance or self._heard_wake_word:
            return
        self._heard_wake_word = True
        if not self._addressed:
            self._addressed = True
            await self._begin_addressed_utterance()

    async def _begin_addressed_utterance(self) -> None:
        self._asr_session = self._asr.start(AudioClip(bytes(self._unaddressed_audio)))
        self._unaddressed_audio = bytearray()
        await self._handler.on_speech_start(self._endpoint_id)

    async def _check_speaker_early(self, frame: AudioFrame) -> None:
        if self._early_speaker_check is None or not self._early_check_seconds:
            return
        self._early_audio += frame.pcm
        self._frames_since_start += 1
        if self._frames_since_start * FRAME_MS < self._early_check_seconds[0] * 1000:
            return
        heard_seconds = self._early_check_seconds.pop(0)
        heard_so_far = AudioClip(bytes(self._early_audio))
        if not self._early_check_seconds:
            self._early_audio = bytearray()
        if not self._addressed:
            return
        speaker = await self._early_speaker_check.identify(
            heard_so_far, heard_seconds=heard_seconds
        )
        if speaker is Speaker.OWNER:
            self._early_check_seconds = []
            self._early_audio = bytearray()
            await self._handler.on_owner_heard(self._endpoint_id)

    async def _on_speech_end(self, clip: AudioClip) -> None:
        self._in_utterance = False
        self._unaddressed_audio = bytearray()
        self._early_check_seconds = []
        self._early_audio = bytearray()
        if not self._addressed:
            return
        assert self._asr_session is not None
        ended_at = self._clock.now()
        ended_at_monotonic = self._clock.monotonic()
        speaker_check = (
            None
            if self._speaker_check is None
            else asyncio.create_task(self._speaker_check.identify(clip), name="speaker-check")
        )
        try:
            text = await self._finish_recognition(clip)
            recognized_at_monotonic = self._clock.monotonic()
            spoken_chars = _count_spoken_chars(text)
            if spoken_chars == 0:
                await self._handler.on_speech_dropped(self._endpoint_id)
                return
            if self._heard_wake_word and spoken_chars <= _NAME_ONLY_MAX_CHARS:
                text = ""
            speaker = Speaker.OWNER if speaker_check is None else await speaker_check
        finally:
            if speaker_check is not None:
                speaker_check.cancel()
        await self._handler.on_utterance(
            Utterance(
                self._endpoint_id,
                text,
                ended_at,
                speaker,
                ended_at_monotonic,
                heard_wake_word=self._heard_wake_word,
                recognized_at_monotonic=recognized_at_monotonic,
            )
        )

    async def _finish_recognition(self, clip: AudioClip) -> str:
        assert self._asr_session is not None
        try:
            text = await self._asr_session.finish(clip)
        except AsrError as error:
            logger.warning("这句没识别出来，丢掉了：%s", error)
            text = ""
        self._asr_session = None
        return text


def _count_spoken_chars(text: str) -> int:
    return sum(1 for char in text if unicodedata.category(char)[0] in ("L", "N"))
