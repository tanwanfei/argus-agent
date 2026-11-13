import logging
import wave
from pathlib import Path

from argus.channels.audio import CHANNELS, SAMPLE_RATE, SAMPLE_WIDTH, AudioClip
from argus.kernel.clock import Clock
from argus.speech.speaker import SpeakerVerifier

logger = logging.getLogger(__name__)


class SavingVerifier:
    def __init__(self, verifier: SpeakerVerifier, directory: Path, clock: Clock) -> None:
        self._verifier = verifier
        self._directory = directory
        self._clock = clock

    def score(self, clip: AudioClip) -> float:
        score = self._verifier.score(clip)
        try:
            self._save(clip, score)
        except OSError as error:
            logger.warning("手机上的这句没存下来：%s", error)
        return score

    def _save(self, clip: AudioClip, score: float) -> None:
        now = self._clock.now()
        name = f"{now:%Y%m%d-%H%M%S}-{now.microsecond // 1000:03d}-{score:.2f}.wav"
        self._directory.mkdir(parents=True, exist_ok=True)
        with wave.open(str(self._directory / name), "wb") as wav_file:
            wav_file.setnchannels(CHANNELS)
            wav_file.setsampwidth(SAMPLE_WIDTH)
            wav_file.setframerate(SAMPLE_RATE)
            wav_file.writeframes(clip.pcm)
