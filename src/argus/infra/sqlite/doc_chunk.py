import sqlite3
from collections import Counter
from collections.abc import Sequence

from argus.infra.sqlite.database import SqliteDatabase
from argus.infra.sqlite.memory_store import check_vector_dimension
from argus.infra.sqlite.vector_extension import serialize_vector
from argus.knowledge.chunking import Chunk
from argus.knowledge.index import StoredChunk
from argus.memory.ports import Vector

_SELECT_DOCUMENT_CHUNK_IDS = "SELECT id FROM doc_chunk WHERE source = ? AND doc_id = ?"
_DELETE_CHUNK = "DELETE FROM doc_chunk WHERE id = ?"
_DELETE_CHUNK_VECTOR = "DELETE FROM doc_chunk_vec WHERE rowid = ?"
_INSERT_CHUNK = """
INSERT INTO doc_chunk (source, doc_id, title, label, ordinal, text)
VALUES (:source, :doc_id, :title, :label, :ordinal, :text)
"""
_INSERT_CHUNK_VECTOR = "INSERT INTO doc_chunk_vec (rowid, embedding) VALUES (?, ?)"

_NEAREST_CHUNKS = """
SELECT chunk.id, chunk.title, chunk.label, chunk.text, nearest.distance
FROM doc_chunk_vec AS nearest
JOIN doc_chunk AS chunk ON chunk.id = nearest.rowid
WHERE nearest.embedding MATCH :query AND k = :limit
ORDER BY nearest.distance
"""

_CHUNKS_CONTAINING = "SELECT id, title, label, text FROM doc_chunk WHERE text LIKE ? ESCAPE '\\'"


class SqliteChunkStore:
    def __init__(self, database: SqliteDatabase) -> None:
        self._database = database

    async def replace_document(
        self, source: str, doc_id: str, title: str, chunks: Sequence[tuple[Chunk, Vector]]
    ) -> None:
        for _, vector in chunks:
            check_vector_dimension(vector)
        rows = [
            (
                {
                    "source": source,
                    "doc_id": doc_id,
                    "title": title,
                    "label": chunk.label,
                    "ordinal": ordinal,
                    "text": chunk.text,
                },
                serialize_vector(vector),
            )
            for ordinal, (chunk, vector) in enumerate(chunks)
        ]

        def replace(connection: sqlite3.Connection) -> None:
            for (chunk_id,) in connection.execute(
                _SELECT_DOCUMENT_CHUNK_IDS, (source, doc_id)
            ).fetchall():
                connection.execute(_DELETE_CHUNK, (chunk_id,))
                connection.execute(_DELETE_CHUNK_VECTOR, (chunk_id,))
            for row, vector_bytes in rows:
                chunk_id = connection.execute(_INSERT_CHUNK, row).lastrowid
                connection.execute(_INSERT_CHUNK_VECTOR, (chunk_id, vector_bytes))

        await self._database.write(replace)

    async def nearest_chunks(
        self, vector: Vector, *, limit: int
    ) -> list[tuple[StoredChunk, float]]:
        if limit < 1:
            raise ValueError(f"limit 至少是 1，收到 {limit}")
        check_vector_dimension(vector)
        query = {"query": serialize_vector(vector), "limit": limit}

        def select_nearest(
            connection: sqlite3.Connection,
        ) -> list[tuple[int, str, str, str, float]]:
            return connection.execute(_NEAREST_CHUNKS, query).fetchall()

        return [
            (StoredChunk(chunk_id, title, label, text), 1.0 - distance)
            for chunk_id, title, label, text, distance in await self._database.read(select_nearest)
        ]

    async def chunks_containing(self, terms: Sequence[str], *, limit: int) -> list[StoredChunk]:
        patterns = [
            "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            for term in terms
        ]

        def select_containing(
            connection: sqlite3.Connection,
        ) -> list[list[tuple[int, str, str, str]]]:
            return [
                connection.execute(_CHUNKS_CONTAINING, (pattern,)).fetchall()
                for pattern in patterns
            ]

        matched_terms: Counter[int] = Counter()
        chunks: dict[int, StoredChunk] = {}
        for rows in await self._database.read(select_containing):
            for chunk_id, title, label, text in rows:
                matched_terms[chunk_id] += 1
                chunks[chunk_id] = StoredChunk(chunk_id, title, label, text)
        ranked = sorted(matched_terms, key=lambda chunk_id: (-matched_terms[chunk_id], chunk_id))
        return [chunks[chunk_id] for chunk_id in ranked[:limit]]
