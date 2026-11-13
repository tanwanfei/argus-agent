from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from argus.channels.audio import FRAME_SAMPLES, SAMPLE_RATE, SAMPLE_WIDTH, AudioClip, AudioFrame

_SPEECH_ON_PROBABILITY = 0.5
_SPEECH_OFF_PROBABILITY = 0.35
_MIN_SPEECH_MS = 250
_CLIP_PRE_ROLL_MS = 500
_TURN_DETECTOR_PRE_ROLL_MS = 200
_LONGEST_PRE_ROLL_MS = max(_CLIP_PRE_ROLL_MS, _TURN_DETECTOR_PRE_ROLL_MS)
_MAX_UTTERANCE_MS = 30_000


def _samples(milliseconds: int) -> int:
    return milliseconds * SAMPLE_RATE // 1000


@dataclass(frozen=True)
class SpeechStart:
    heard_so_far: AudioClip


@dataclass(frozen=True)
class SpeechEnd:
    clip: AudioClip


VadEvent = SpeechStart | SpeechEnd


class Vad(Protocol):
    def feed(self, frame: AudioFrame) -> VadEvent | None: ...


class SpeechDetector(Protocol):
    @property
    def window_samples(self) -> int: ...

    def speech_probability(self, window_pcm: bytes) -> float: ...


class TurnVerdict(StrEnum):
    FINISHED = "finished"
    UNFINISHED = "unfinished"
    INVALID = "invalid"


class TurnDetector(Protocol):
    def judge(self, clip: AudioClip) -> TurnVerdict: ...


class TurnTakingVad:
    def __init__(
        self,
        speech_detector: SpeechDetector,
        turn_detector: TurnDetector | None,
        *,
        ask_turn_after_silence_ms: int,
        max_silence_ms: int,
    ) -> None:
        if not 0 < ask_turn_after_silence_ms < max_silence_ms:
            raise ValueError(
                f"要 0 < ask_turn_after_silence_ms < max_silence_ms，"
                f"收到 {ask_turn_after_silence_ms} 和 {max_silence_ms}"
            )
        if speech_detector.window_samples < FRAME_SAMPLES:
            raise ValueError(
                f"说话概率模型的窗口不能短于一帧（{FRAME_SAMPLES} 个采样），"
                f"收到 {speech_detector.window_samples}"
            )
        self._speech_detector = speech_detector
        self._turn_detector = turn_detector
        self._window_samples = speech_detector.window_samples
        self._ask_turn_after_samples = _samples(ask_turn_after_silence_ms)
        self._max_silence_samples = _samples(max_silence_ms)

        self._audio = bytearray()
        self._audio_start = 0
        self._next_window_start = 0
        self._run_start: int | None = None
        self._run_is_speech = False
        self._utterance_onset: int | None = None
        self._silence_start: int | None = None
        self._asked_turn_detector = False

    def feed(self, frame: AudioFrame) -> VadEvent | None:
        self._audio += frame.pcm
        fed_until = self._audio_start + len(self._audio) // SAMPLE_WIDTH
        if fed_until - self._next_window_start < self._window_samples:
            return None
        return self._process_next_window()

    def _process_next_window(self) -> VadEvent | None:
        window_start = self._next_window_start
        window_end = window_start + self._window_samples
        self._next_window_start = window_end
        probability = self._speech_detector.speech_probability(
            self._audio_between(window_start, window_end)
        )

        event: VadEvent | None = self._track_speech_run(probability, window_start, window_end)
        if event is None and self._utterance_onset is not None:
            event = self._check_utterance_end(window_end)
        if self._utterance_onset is None:
            self._drop_audio_before_pre_roll()
        return event

    def _track_speech_run(
        self, probability: float, window_start: int, window_end: int
    ) -> SpeechStart | None:
        if self._run_start is None:
            if probability >= _SPEECH_ON_PROBABILITY:
                self._run_start = window_start
                self._run_is_speech = False
        elif probability < _SPEECH_OFF_PROBABILITY:
            if self._run_is_speech:
                self._silence_start = window_start
                self._asked_turn_detector = False
            self._run_start = None
            return None

        if self._run_start is None or self._run_is_speech:
            return None
        if window_end - self._run_start < _samples(_MIN_SPEECH_MS):
            return None
        self._run_is_speech = True
        if self._utterance_onset is None:
            self._utterance_onset = self._run_start
            return SpeechStart(AudioClip(self._audio_since(self._before_onset(_CLIP_PRE_ROLL_MS))))
        self._silence_start = None
        return None

    def _check_utterance_end(self, window_end: int) -> SpeechEnd | None:
        assert self._utterance_onset is not None
        if window_end - self._utterance_onset >= _samples(_MAX_UTTERANCE_MS):
            return self._end_utterance(window_end)
        if self._silence_start is None or self._run_start is not None:
            return None

        silence_samples = window_end - self._silence_start
        if silence_samples >= self._max_silence_samples:
            return self._end_utterance(window_end)
        if (
            self._turn_detector is not None
            and not self._asked_turn_detector
            and silence_samples >= self._ask_turn_after_samples
        ):
            self._asked_turn_detector = True
            heard_so_far = AudioClip(
                self._audio_between(self._before_onset(_TURN_DETECTOR_PRE_ROLL_MS), window_end)
            )
            if self._turn_detector.judge(heard_so_far) is TurnVerdict.FINISHED:
                return self._end_utterance(window_end)
        return None

    def _end_utterance(self, window_end: int) -> SpeechEnd:
        clip = AudioClip(self._audio_between(self._before_onset(_CLIP_PRE_ROLL_MS), window_end))
        self._utterance_onset = None
        self._silence_start = None
        self._run_start = None
        return SpeechEnd(clip)

    def _before_onset(self, pre_roll_ms: int) -> int:
        assert self._utterance_onset is not None
        return max(self._audio_start, self._utterance_onset - _samples(pre_roll_ms))

    def _drop_audio_before_pre_roll(self) -> None:
        earliest_onset = self._run_start if self._run_start is not None else self._next_window_start
        keep_from = earliest_onset - _samples(_LONGEST_PRE_ROLL_MS)
        if keep_from > self._audio_start:
            del self._audio[: (keep_from - self._audio_start) * SAMPLE_WIDTH]
            self._audio_start = keep_from

    def _audio_since(self, start: int) -> bytes:
        return bytes(self._audio[(start - self._audio_start) * SAMPLE_WIDTH :])

    def _audio_between(self, start: int, end: int) -> bytes:
        start_byte = (start - self._audio_start) * SAMPLE_WIDTH
        end_byte = (end - self._audio_start) * SAMPLE_WIDTH
        return bytes(self._audio[start_byte:end_byte])
