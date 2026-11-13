import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from argus.memory.records import EpisodeMatch

DEFAULT_HALF_LIFE_DAYS = 30.0

_SECONDS_PER_DAY = 86400.0


@dataclass(frozen=True)
class RankedEpisode:
    match: EpisodeMatch
    score: float


def decay_factor(age: timedelta, *, half_life_days: float) -> float:
    _check_half_life(half_life_days)
    age_days = max(age.total_seconds(), 0.0) / _SECONDS_PER_DAY
    return math.pow(0.5, age_days / half_life_days)


def rank_episodes(
    matches: Sequence[EpisodeMatch],
    *,
    now: datetime,
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
) -> list[RankedEpisode]:
    _check_half_life(half_life_days)
    ranked = [
        RankedEpisode(
            match=match,
            score=match.similarity
            * match.episode.weight
            * decay_factor(now - match.episode.ts, half_life_days=half_life_days),
        )
        for match in matches
    ]
    return sorted(
        ranked,
        key=lambda item: (-item.score, -item.match.episode.ts.timestamp(), item.match.episode.id),
    )


def _check_half_life(half_life_days: float) -> None:
    if half_life_days <= 0:
        raise ValueError(f"半衰期要大于 0 天，收到 {half_life_days}")
