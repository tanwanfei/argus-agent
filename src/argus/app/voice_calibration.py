import math
from collections.abc import Sequence
from dataclasses import dataclass

from argus.kernel.tools import Speaker
from argus.speech.speaker import speaker_for

TARGET_OWNER_REJECT_PERCENT = 5
SHORT_SENTENCE_SECONDS = 3.0


@dataclass(frozen=True)
class ThresholdRates:
    threshold: float
    owner_rejected: float
    other_accepted: float


def rates_at(
    threshold: float, owner_scores: Sequence[float], other_scores: Sequence[float]
) -> ThresholdRates:
    if not owner_scores or not other_scores:
        raise ValueError("你的、别人的句子都要有")
    owner_rejected = sum(speaker_for(score, threshold) is Speaker.OTHER for score in owner_scores)
    other_accepted = sum(speaker_for(score, threshold) is Speaker.OWNER for score in other_scores)
    return ThresholdRates(
        threshold, owner_rejected / len(owner_scores), other_accepted / len(other_scores)
    )


def highest_threshold_within_target(owner_scores: Sequence[float]) -> float:
    if not owner_scores:
        raise ValueError("你的句子要有")
    ranked = sorted(owner_scores)
    allowed_rejections = (len(ranked) * TARGET_OWNER_REJECT_PERCENT - 1) // 100
    return math.floor(ranked[allowed_rejections] * 100) / 100
