import sqlite3

from argus.infra.sqlite.database import SqliteDatabase
from argus.infra.sqlite.memory_store import check_vector_dimension, episode_from_row
from argus.infra.sqlite.vector_extension import serialize_vector
from argus.memory.ports import Vector
from argus.memory.records import EpisodeMatch

_NEAREST_EPISODES = """
SELECT episode.id, episode.ts, episode.summary, episode.weight, episode.source_ref,
       episode.created_at, nearest.distance
FROM mem_episode_vec AS nearest
JOIN mem_episode AS episode ON episode.id = nearest.rowid
WHERE nearest.embedding MATCH :query AND k = :limit
ORDER BY nearest.distance
"""


class SqliteVectorIndex:
    def __init__(self, database: SqliteDatabase) -> None:
        self._database = database

    async def nearest_episodes(self, vector: Vector, *, limit: int) -> list[EpisodeMatch]:
        if limit < 1:
            raise ValueError(f"limit 至少是 1，收到 {limit}")
        check_vector_dimension(vector)
        query = {"query": serialize_vector(vector), "limit": limit}

        def select_nearest(
            connection: sqlite3.Connection,
        ) -> list[tuple[int, str, str, float, str | None, str, float]]:
            return connection.execute(_NEAREST_EPISODES, query).fetchall()

        return [
            EpisodeMatch(
                episode=episode_from_row((episode_id, ts, summary, weight, source_ref, created_at)),
                similarity=1.0 - distance,
            )
            for episode_id, ts, summary, weight, source_ref, created_at, distance in (
                await self._database.read(select_nearest)
            )
        ]
