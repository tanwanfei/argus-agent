import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, time

from argus.kernel.clock import Clock
from argus.memory.conflict import ConflictAction, resolve_conflict
from argus.memory.days import days_mentioned
from argus.memory.extractor import (
    ConversationTurn,
    Extracted,
    ExtractedEpisode,
    ExtractedFact,
)
from argus.memory.habits import habit_matches
from argus.memory.matching import episodes_about, facts_about, matching_facts
from argus.memory.merge import merge_recalled
from argus.memory.ports import Embedder, MemoryStore, Vector, VectorIndex
from argus.memory.profile import ProfileEntry, plan_profile_import
from argus.memory.ranking import DEFAULT_HALF_LIFE_DAYS, rank_episodes
from argus.memory.records import Fact, FactSource, MemoryItem, NewEpisode, NewFact
from argus.memory.render import episode_item, fact_item, habit_item
from argus.memory.write_gate import WriteReason, admit_episode, admit_fact

logger = logging.getLogger(__name__)

DEFAULT_MIN_SIMILARITY = 0.5
RESIDENT_MAX_TOKENS = 1500
MIN_EXTRACTED_CONFIDENCE = 0.5

_EPISODE_CANDIDATES = 15
_EPISODES_RECALLED = 5
_EPISODES_ON_DAYS_RECALLED = 10

EXPLICIT_CONFIDENCE = 1.0

SAME_EPISODE_SIMILARITY = 0.8
_SAME_EPISODE_CANDIDATES = 5


@dataclass(frozen=True)
class RememberOutcome:
    fact: Fact | None
    replaced: tuple[Fact, ...]


@dataclass(frozen=True)
class StoredExtraction:
    facts_written: int
    episodes_written: int
    discarded: int


@dataclass(frozen=True)
class ProfileImport:
    added: tuple[ProfileEntry, ...]
    removed: tuple[ProfileEntry, ...]
    unchanged: int


@dataclass(frozen=True)
class ForgetOutcome:
    facts_deleted: int
    episodes_deleted: int


class Memory:
    def __init__(
        self,
        *,
        store: MemoryStore,
        clock: Clock,
        embedder: Embedder | None = None,
        index: VectorIndex | None = None,
        half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
        min_similarity: float = DEFAULT_MIN_SIMILARITY,
    ) -> None:
        self._store = store
        self._clock = clock
        self._embedder = embedder
        self._index = index
        self._half_life_days = half_life_days
        self._min_similarity = min_similarity
        self._write_lock = asyncio.Lock()

    async def remember(
        self, *, subject: str, attribute: str, value: str, replaces: bool
    ) -> RememberOutcome:
        decision = admit_fact(
            subject=subject, attribute=attribute, value=value, reason=WriteReason.EXPLICIT
        )
        if not decision.write:
            raise ValueError(decision.explanation)
        now = self._clock.now()
        new_fact = NewFact(
            subject=subject.strip(),
            attribute=attribute.strip(),
            value=value.strip(),
            confidence=EXPLICIT_CONFIDENCE,
            source=FactSource.REMEMBER,
            valid_from=now,
            created_at=now,
        )
        async with self._write_lock:
            existing_facts = await self._store.list_facts()
            resolution = resolve_conflict(new_fact, existing_facts, replaces=replaces)
            if resolution.action is ConflictAction.ALREADY_KNOWN:
                return RememberOutcome(fact=None, replaced=())
            written = await self._store.add_fact(new_fact, supersedes=resolution.supersedes)
        replaced = tuple(fact for fact in existing_facts if fact.id in resolution.supersedes)
        return RememberOutcome(fact=written, replaced=replaced)

    async def undo_remember(self, fact_id: int) -> bool:
        async with self._write_lock:
            deleted = await self._store.delete_facts([fact_id], restore_superseded=True)
        return deleted == 1

    async def resident(self) -> list[MemoryItem]:
        facts = await self._store.list_facts()
        return merge_recalled(
            facts=[fact_item(fact) for fact in facts],
            habits=[],
            episodes=[],
            max_tokens=RESIDENT_MAX_TOKENS,
        )

    async def search(self, query: str) -> list[MemoryItem]:
        if not query.strip():
            facts = matching_facts("", await self._store.list_facts())
            return merge_recalled(facts=[fact_item(fact) for fact in facts], habits=[], episodes=[])
        return await self.recall(query)

    async def recall(self, query: str) -> list[MemoryItem]:
        if not query.strip():
            return []
        now = self._clock.now()
        facts, episodes, habits, episodes_on_days = await asyncio.gather(
            self._recall_facts(query),
            self._recall_episodes(query, now),
            self._recall_habits(now),
            self._recall_episodes_on_days(query, now),
        )
        return merge_recalled(facts=facts, habits=habits, episodes=[*episodes_on_days, *episodes])

    async def _recall_facts(self, query: str) -> list[MemoryItem]:
        facts = matching_facts(query, await self._store.list_facts())
        return [fact_item(fact) for fact in facts]

    async def _recall_episodes(self, query: str, now: datetime) -> list[MemoryItem]:
        if self._embedder is None or self._index is None:
            return []
        matches = await self._index.nearest_episodes(
            await self._embedder.embed(query), limit=_EPISODE_CANDIDATES
        )
        related = [match for match in matches if match.similarity >= self._min_similarity]
        ranked = rank_episodes(related, now=now, half_life_days=self._half_life_days)
        return [episode_item(item.match.episode) for item in ranked[:_EPISODES_RECALLED]]

    async def _recall_episodes_on_days(self, query: str, now: datetime) -> list[MemoryItem]:
        days = days_mentioned(query, now.date())
        if not days:
            return []
        episodes = await self._store.list_episodes_on(days)
        return [episode_item(episode) for episode in episodes[:_EPISODES_ON_DAYS_RECALLED]]

    async def _recall_habits(self, now: datetime) -> list[MemoryItem]:
        return [
            habit_item(habit)
            for habit in await self._store.list_habits()
            if habit_matches(habit.condition, now)
        ]

    async def forget(self, topic: str) -> ForgetOutcome:
        async with self._write_lock:
            facts = facts_about(topic, await self._store.list_facts(include_superseded=True))
            episodes = episodes_about(topic, await self._store.list_episodes())
            facts_deleted = await self._store.delete_facts(
                [fact.id for fact in facts], restore_superseded=False
            )
            episodes_deleted = await self._store.delete_episodes([e.id for e in episodes])
        logger.info("忘掉了 %d 条事实、%d 件经历", facts_deleted, episodes_deleted)
        return ForgetOutcome(facts_deleted=facts_deleted, episodes_deleted=episodes_deleted)

    async def store_extracted(
        self, extracted: Extracted, *, turn: ConversationTurn
    ) -> StoredExtraction:
        async with self._write_lock:
            facts_written, facts_discarded = await self._write_extracted_facts(
                extracted.facts, turn
            )
            episodes_written, episodes_discarded = await self._write_extracted_episodes(
                extracted.episodes, turn
            )
        discarded = facts_discarded + episodes_discarded
        logger.info(
            "抽取结果写进库：%d 条事实、%d 件经历，丢掉 %d 条",
            facts_written,
            episodes_written,
            discarded,
        )
        return StoredExtraction(facts_written, episodes_written, discarded)

    async def _write_extracted_facts(
        self, extracted_facts: Sequence[ExtractedFact], turn: ConversationTurn
    ) -> tuple[int, int]:
        written_count = discarded_count = 0
        existing_facts = await self._store.list_facts()
        for extracted_fact in extracted_facts:
            if extracted_fact.confidence < MIN_EXTRACTED_CONFIDENCE:
                discarded_count += 1
                continue
            decision = admit_fact(
                subject=extracted_fact.subject,
                attribute=extracted_fact.attribute,
                value=extracted_fact.value,
                reason=extracted_fact.reason,
            )
            if not decision.write:
                discarded_count += 1
                continue
            new_fact = NewFact(
                subject=extracted_fact.subject.strip(),
                attribute=extracted_fact.attribute.strip(),
                value=extracted_fact.value.strip(),
                confidence=extracted_fact.confidence,
                source=FactSource.EXTRACT,
                valid_from=turn.happened_at,
                created_at=self._clock.now(),
            )
            resolution = resolve_conflict(
                new_fact, existing_facts, replaces=extracted_fact.replaces
            )
            if resolution.action is ConflictAction.ALREADY_KNOWN:
                discarded_count += 1
                continue
            written = await self._store.add_fact(new_fact, supersedes=resolution.supersedes)
            written_count += 1
            existing_facts = [
                fact for fact in existing_facts if fact.id not in resolution.supersedes
            ] + [written]
        return written_count, discarded_count

    async def _write_extracted_episodes(
        self, extracted_episodes: Sequence[ExtractedEpisode], turn: ConversationTurn
    ) -> tuple[int, int]:
        if turn.trace_id is not None and await self._store.has_episodes_from(turn.trace_id):
            return 0, len(extracted_episodes)
        written_count = discarded_count = 0
        for extracted_episode in extracted_episodes:
            decision = admit_episode(
                summary=extracted_episode.summary, reason=extracted_episode.reason
            )
            if not decision.write or self._embedder is None:
                discarded_count += 1
                continue
            happened_at = (
                turn.happened_at
                if extracted_episode.when is None
                else datetime.combine(
                    extracted_episode.when, time(12, 0), tzinfo=turn.happened_at.tzinfo
                )
            )
            summary = extracted_episode.summary.strip()
            episode = NewEpisode(
                ts=happened_at,
                summary=summary,
                weight=1.0,
                source_ref=turn.trace_id,
                created_at=turn.happened_at,
            )
            vector = await self._embedder.embed(summary)
            same_episode_ids = await self._same_episode_ids(happened_at, vector)
            await self._store.add_episode(episode, vector)
            if same_episode_ids:
                await self._store.delete_episodes(same_episode_ids)
            written_count += 1
        return written_count, discarded_count

    async def _same_episode_ids(self, happened_at: datetime, vector: Vector) -> list[int]:
        if self._index is None:
            return []
        matches = await self._index.nearest_episodes(vector, limit=_SAME_EPISODE_CANDIDATES)
        happened_on = happened_at.date()
        return [
            match.episode.id
            for match in matches
            if match.similarity >= SAME_EPISODE_SIMILARITY
            and match.episode.ts.astimezone(happened_at.tzinfo).date() == happened_on
        ]

    async def import_profile(
        self, entries: Sequence[ProfileEntry], *, dry_run: bool = False
    ) -> ProfileImport:
        async with self._write_lock:
            plan = plan_profile_import(
                entries, await self._store.list_facts(include_superseded=True)
            )
            now = self._clock.now()
            if not dry_run:
                for entry in plan.to_add:
                    await self._store.add_fact(
                        NewFact(
                            subject=entry.subject,
                            attribute=entry.attribute,
                            value=entry.value,
                            confidence=EXPLICIT_CONFIDENCE,
                            source=FactSource.PROFILE,
                            valid_from=now,
                            created_at=now,
                        )
                    )
                await self._store.delete_facts(
                    [fact.id for fact in plan.to_remove], restore_superseded=False
                )
        return ProfileImport(
            added=plan.to_add,
            removed=tuple(
                ProfileEntry(fact.subject, fact.attribute, fact.value) for fact in plan.to_remove
            ),
            unchanged=plan.unchanged,
        )
