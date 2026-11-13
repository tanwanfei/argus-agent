import json
import logging
import sqlite3
from collections.abc import Sequence
from datetime import date, datetime

from argus.infra.sqlite.database import SqliteDatabase
from argus.infra.sqlite.vector_extension import serialize_vector
from argus.memory.ports import EPISODE_VECTOR_DIMENSION, Vector
from argus.memory.records import (
    Episode,
    Fact,
    FactSource,
    Habit,
    NewEpisode,
    NewFact,
    NewHabit,
)

logger = logging.getLogger(__name__)

_INSERT_FACT = """
INSERT INTO mem_fact (subject, attribute, value, confidence, source, valid_from, created_at)
VALUES (:subject, :attribute, :value, :confidence, :source, :valid_from, :created_at)
"""

_MARK_SUPERSEDED = "UPDATE mem_fact SET superseded_by = ? WHERE superseded_by IS NULL AND id = ?"

_SELECT_FACTS = """
SELECT id, subject, attribute, value, confidence, source, valid_from, superseded_by, created_at
FROM mem_fact
WHERE (:subject IS NULL OR subject = :subject)
  AND (:attribute IS NULL OR attribute = :attribute)
  AND (:include_superseded OR superseded_by IS NULL)
ORDER BY id
"""

_DELETE_FACT = "DELETE FROM mem_fact WHERE id = ?"

_RESTORE_SUPERSEDED = "UPDATE mem_fact SET superseded_by = NULL WHERE superseded_by = ?"

type FactRow = tuple[int, str, str, str, float, str, str, int | None, str]

_INSERT_EPISODE = """
INSERT INTO mem_episode (ts, summary, weight, source_ref, created_at)
VALUES (:ts, :summary, :weight, :source_ref, :created_at)
"""

_SELECT_EPISODES = (
    "SELECT id, ts, summary, weight, source_ref, created_at FROM mem_episode ORDER BY id"
)

_SELECT_EPISODES_ON_DAYS = """
SELECT id, ts, summary, weight, source_ref, created_at FROM mem_episode
WHERE substr(ts, 1, 10) IN (SELECT value FROM json_each(:days))
ORDER BY ts, id
"""

_INSERT_HABIT = """
INSERT INTO mem_habit (condition_json, statement, support, last_seen)
VALUES (:condition_json, :statement, :support, :last_seen)
"""

_INSERT_HABIT_FROM = """
INSERT INTO mem_habit (condition_json, statement, support, last_seen, source)
VALUES (:condition_json, :statement, :support, :last_seen, :source)
"""

_DELETE_ENABLED_HABITS_FROM = "DELETE FROM mem_habit WHERE source = ? AND enabled = 1"

_SELECT_ENABLED_HABITS = """
SELECT id, condition_json, statement, support, last_seen FROM mem_habit WHERE enabled = 1 ORDER BY id
"""

_EPISODE_FROM_SOURCE = "SELECT 1 FROM mem_episode WHERE source_ref = ? LIMIT 1"

_DELETE_EPISODE = "DELETE FROM mem_episode WHERE id = ?"

_DELETE_EPISODE_VECTOR = "DELETE FROM mem_episode_vec WHERE rowid = ?"
_INSERT_EPISODE_VECTOR = "INSERT INTO mem_episode_vec (rowid, embedding) VALUES (?, ?)"
_COUNT_VECTORS_WITHOUT_EPISODE = (
    "SELECT count(*) FROM mem_episode_vec WHERE rowid NOT IN (SELECT id FROM mem_episode)"
)
_COUNT_EPISODES_WITHOUT_VECTOR = (
    "SELECT count(*) FROM mem_episode WHERE id NOT IN (SELECT rowid FROM mem_episode_vec)"
)

type EpisodeRow = tuple[int, str, str, float, str | None, str]


def episode_from_row(row: EpisodeRow) -> Episode:
    episode_id, ts, summary, weight, source_ref, created_at = row
    return Episode(
        id=episode_id,
        ts=datetime.fromisoformat(ts),
        summary=summary,
        weight=weight,
        source_ref=source_ref,
        created_at=datetime.fromisoformat(created_at),
    )


def check_vector_dimension(vector: Vector) -> None:
    if len(vector) != EPISODE_VECTOR_DIMENSION:
        raise ValueError(
            f"向量要 {EPISODE_VECTOR_DIMENSION} 维（mem_episode_vec 的列宽），收到 {len(vector)} 维"
        )


class SqliteMemoryStore:
    def __init__(self, database: SqliteDatabase) -> None:
        self._database = database

    async def add_fact(self, fact: NewFact, *, supersedes: Sequence[int] = ()) -> Fact:
        row = {
            "subject": fact.subject,
            "attribute": fact.attribute,
            "value": fact.value,
            "confidence": fact.confidence,
            "source": fact.source.value,
            "valid_from": fact.valid_from.isoformat(),
            "created_at": fact.created_at.isoformat(),
        }
        superseded_ids = sorted(set(supersedes))

        def insert_and_supersede(connection: sqlite3.Connection) -> int:
            new_id = connection.execute(_INSERT_FACT, row).lastrowid
            assert new_id is not None
            for old_id in superseded_ids:
                if connection.execute(_MARK_SUPERSEDED, (new_id, old_id)).rowcount != 1:
                    raise ValueError(f"第 {old_id} 条记忆不是现在有效的，不能被取代")
            return new_id

        new_id = await self._database.write(insert_and_supersede)
        return Fact(
            subject=fact.subject,
            attribute=fact.attribute,
            value=fact.value,
            confidence=fact.confidence,
            source=fact.source,
            valid_from=fact.valid_from,
            created_at=fact.created_at,
            id=new_id,
            superseded_by=None,
        )

    async def list_facts(
        self,
        *,
        subject: str | None = None,
        attribute: str | None = None,
        include_superseded: bool = False,
    ) -> list[Fact]:
        filters = {
            "subject": subject,
            "attribute": attribute,
            "include_superseded": int(include_superseded),
        }

        def select_facts(connection: sqlite3.Connection) -> list[FactRow]:
            return connection.execute(_SELECT_FACTS, filters).fetchall()

        return [
            Fact(
                id=fact_id,
                subject=row_subject,
                attribute=row_attribute,
                value=value,
                confidence=confidence,
                source=FactSource(source),
                valid_from=datetime.fromisoformat(valid_from),
                superseded_by=superseded_by,
                created_at=datetime.fromisoformat(created_at),
            )
            for (
                fact_id,
                row_subject,
                row_attribute,
                value,
                confidence,
                source,
                valid_from,
                superseded_by,
                created_at,
            ) in await self._database.read(select_facts)
        ]

    async def delete_facts(self, fact_ids: Sequence[int], *, restore_superseded: bool) -> int:
        def delete(connection: sqlite3.Connection) -> int:
            deleted_count = 0
            for fact_id in fact_ids:
                if connection.execute(_DELETE_FACT, (fact_id,)).rowcount == 1:
                    deleted_count += 1
                    if restore_superseded:
                        connection.execute(_RESTORE_SUPERSEDED, (fact_id,))
            return deleted_count

        return await self._database.write(delete)

    async def add_episode(self, episode: NewEpisode, vector: Vector) -> Episode:
        check_vector_dimension(vector)
        row = {
            "ts": episode.ts.isoformat(),
            "summary": episode.summary,
            "weight": episode.weight,
            "source_ref": episode.source_ref,
            "created_at": episode.created_at.isoformat(),
        }
        vector_bytes = serialize_vector(vector)

        def insert_episode_and_vector(connection: sqlite3.Connection) -> int:
            new_id = connection.execute(_INSERT_EPISODE, row).lastrowid
            assert new_id is not None
            connection.execute(_DELETE_EPISODE_VECTOR, (new_id,))
            connection.execute(_INSERT_EPISODE_VECTOR, (new_id, vector_bytes))
            return new_id

        new_id = await self._database.write(insert_episode_and_vector)
        return Episode(
            ts=episode.ts,
            summary=episode.summary,
            weight=episode.weight,
            source_ref=episode.source_ref,
            created_at=episode.created_at,
            id=new_id,
        )

    async def warn_if_vectors_mismatch(self) -> None:
        def count_mismatches(connection: sqlite3.Connection) -> tuple[int, int]:
            (vectors_without_episode,) = connection.execute(
                _COUNT_VECTORS_WITHOUT_EPISODE
            ).fetchone()
            (episodes_without_vector,) = connection.execute(
                _COUNT_EPISODES_WITHOUT_VECTOR
            ).fetchone()
            return vectors_without_episode, episodes_without_vector

        try:
            vectors_without_episode, episodes_without_vector = await self._database.read(
                count_mismatches
            )
        except Exception:
            logger.exception("经历和向量对不对得上，没查成")
            return
        if vectors_without_episode or episodes_without_vector:
            logger.warning(
                "经历和向量对不上：%d 条向量找不到经历，%d 件经历没有向量（没有向量的搜不出来）",
                vectors_without_episode,
                episodes_without_vector,
            )

    async def has_episodes_from(self, source_ref: str) -> bool:
        def exists(connection: sqlite3.Connection) -> bool:
            return connection.execute(_EPISODE_FROM_SOURCE, (source_ref,)).fetchone() is not None

        return await self._database.read(exists)

    async def list_episodes(self) -> list[Episode]:
        def select_episodes(connection: sqlite3.Connection) -> list[EpisodeRow]:
            return connection.execute(_SELECT_EPISODES).fetchall()

        return [episode_from_row(row) for row in await self._database.read(select_episodes)]

    async def list_episodes_on(self, days: Sequence[date]) -> list[Episode]:
        if not days:
            return []
        days_json = json.dumps([day.isoformat() for day in days])

        def select_episodes_on_days(connection: sqlite3.Connection) -> list[EpisodeRow]:
            return connection.execute(_SELECT_EPISODES_ON_DAYS, {"days": days_json}).fetchall()

        rows = await self._database.read(select_episodes_on_days)
        return [episode_from_row(row) for row in rows]

    async def delete_episodes(self, episode_ids: Sequence[int]) -> int:
        def delete(connection: sqlite3.Connection) -> int:
            deleted_count = 0
            for episode_id in episode_ids:
                deleted_count += connection.execute(_DELETE_EPISODE, (episode_id,)).rowcount
                connection.execute(_DELETE_EPISODE_VECTOR, (episode_id,))
            return deleted_count

        return await self._database.write(delete)

    async def add_habit(self, habit: NewHabit) -> Habit:
        row = _habit_row(habit)

        def insert(connection: sqlite3.Connection) -> int:
            new_id = connection.execute(_INSERT_HABIT, row).lastrowid
            assert new_id is not None
            return new_id

        new_id = await self._database.write(insert)
        return Habit(
            condition=habit.condition,
            statement=habit.statement,
            support=habit.support,
            last_seen=habit.last_seen,
            id=new_id,
            enabled=True,
        )

    async def replace_habits(self, source: str, habits: Sequence[NewHabit]) -> None:
        rows = [{**_habit_row(habit), "source": source} for habit in habits]

        def replace(connection: sqlite3.Connection) -> None:
            connection.execute(_DELETE_ENABLED_HABITS_FROM, (source,))
            connection.executemany(_INSERT_HABIT_FROM, rows)

        await self._database.write(replace)

    async def list_habits(self) -> list[Habit]:
        def select_habits(connection: sqlite3.Connection) -> list[tuple[int, str, str, int, str]]:
            return connection.execute(_SELECT_ENABLED_HABITS).fetchall()

        return [
            Habit(
                id=habit_id,
                condition=json.loads(condition_json),
                statement=statement,
                support=support,
                last_seen=datetime.fromisoformat(last_seen),
                enabled=True,
            )
            for habit_id, condition_json, statement, support, last_seen in await self._database.read(
                select_habits
            )
        ]


def _habit_row(habit: NewHabit) -> dict[str, object]:
    if habit.support < 1:
        raise ValueError(f"习惯的依据次数至少是 1，收到 {habit.support}")
    return {
        "condition_json": json.dumps(habit.condition, ensure_ascii=False),
        "statement": habit.statement,
        "support": habit.support,
        "last_seen": habit.last_seen.isoformat(),
    }
