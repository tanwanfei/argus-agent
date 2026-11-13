from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any


class FactSource(StrEnum):
    REMEMBER = "remember"
    EXTRACT = "extract"
    PROFILE = "profile"


@dataclass(frozen=True)
class NewFact:
    subject: str
    attribute: str
    value: str
    confidence: float
    source: FactSource
    valid_from: datetime
    created_at: datetime


@dataclass(frozen=True)
class Fact(NewFact):
    id: int
    superseded_by: int | None


@dataclass(frozen=True)
class NewEpisode:
    ts: datetime
    summary: str
    weight: float
    source_ref: str | None
    created_at: datetime


@dataclass(frozen=True)
class Episode(NewEpisode):
    id: int


@dataclass(frozen=True)
class EpisodeMatch:
    episode: Episode
    similarity: float


@dataclass(frozen=True)
class NewHabit:
    condition: Mapping[str, Any]
    statement: str
    support: int
    last_seen: datetime


@dataclass(frozen=True)
class Habit(NewHabit):
    id: int
    enabled: bool


class MemoryKind(StrEnum):
    FACT = "fact"
    HABIT = "habit"
    EPISODE = "episode"


@dataclass(frozen=True)
class MemoryItem:
    kind: MemoryKind
    text: str
