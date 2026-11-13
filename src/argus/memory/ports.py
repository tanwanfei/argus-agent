from collections.abc import Sequence
from datetime import date
from typing import Protocol

from argus.memory.records import (
    Episode,
    EpisodeMatch,
    Fact,
    Habit,
    NewEpisode,
    NewFact,
    NewHabit,
)

type Vector = tuple[float, ...]

EPISODE_VECTOR_DIMENSION = 512


class Embedder(Protocol):
    dimension: int

    async def embed(self, text: str) -> Vector: ...


class MemoryStore(Protocol):
    async def add_fact(self, fact: NewFact, *, supersedes: Sequence[int] = ()) -> Fact: ...

    async def list_facts(
        self,
        *,
        subject: str | None = None,
        attribute: str | None = None,
        include_superseded: bool = False,
    ) -> list[Fact]: ...

    async def delete_facts(self, fact_ids: Sequence[int], *, restore_superseded: bool) -> int: ...

    async def add_episode(self, episode: NewEpisode, vector: Vector) -> Episode: ...

    async def add_habit(self, habit: NewHabit) -> Habit: ...

    async def replace_habits(self, source: str, habits: Sequence[NewHabit]) -> None: ...

    async def list_habits(self) -> list[Habit]: ...

    async def has_episodes_from(self, source_ref: str) -> bool: ...

    async def list_episodes(self) -> list[Episode]: ...

    async def list_episodes_on(self, days: Sequence[date]) -> list[Episode]: ...

    async def delete_episodes(self, episode_ids: Sequence[int]) -> int: ...


class VectorIndex(Protocol):
    async def nearest_episodes(self, vector: Vector, *, limit: int) -> list[EpisodeMatch]: ...
