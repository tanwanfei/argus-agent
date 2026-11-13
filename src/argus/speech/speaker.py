import logging
from typing import Protocol

from argus.channels.audio import AudioClip
from argus.kernel.tools import Speaker
from argus.speech.inference import SerialInference

logger = logging.getLogger(__name__)

_SAID_AS = {Speaker.OWNER: "你", Speaker.OTHER: "不是你"}


class SpeakerVerifier(Protocol):
    def score(self, clip: AudioClip) -> float: ...


def speaker_for(score: float, threshold: float) -> Speaker:
    return Speaker.OWNER if score >= threshold else Speaker.OTHER


class SpeakerCheck:
    def __init__(self, verifier: SpeakerVerifier | None, threshold: float) -> None:
        self._verifier = verifier
        self._threshold = threshold
        self._serial = SerialInference()
        self._when_unsure = Speaker.OTHER
        self._log_prefix = "说话人"

    def for_app(self, verifier: SpeakerVerifier | None, threshold: float) -> "SpeakerCheck":
        app_check = SpeakerCheck(verifier, threshold)
        app_check._serial = self._serial
        app_check._when_unsure = Speaker.OWNER
        app_check._log_prefix = "App 说话人"
        return app_check

    async def identify(self, clip: AudioClip, *, heard_seconds: float | None = None) -> Speaker:
        verifier = self._verifier
        if verifier is None:
            return self._when_unsure
        try:
            score = await self._serial.run(lambda: verifier.score(clip), skipped_result=-1.0)
        except Exception:
            logger.exception("声纹比对出错，这句当%s", _SAID_AS[self._when_unsure])
            return self._when_unsure
        speaker = speaker_for(score, self._threshold)
        if heard_seconds is None:
            logger.info("%s：%s（%.2f）", self._log_prefix, speaker.value, score)
        else:
            logger.info(
                "%s（开口 %g 秒）：%s（%.2f）",
                self._log_prefix,
                heard_seconds,
                speaker.value,
                score,
            )
        return speaker
