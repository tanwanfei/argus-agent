import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Protocol

from argus.knowledge.chunking import Chunk, chunk_document
from argus.knowledge.sources import Document
from argus.memory.ports import Embedder, Vector

MIN_SIMILARITY = 0.5
MAX_HITS = 5
_CANDIDATES_PER_PATH = 10
_RRF_K = 60
_TERM_SEPARATORS = " \t\n，,、。？?！!；;：:"


@dataclass(frozen=True)
class StoredChunk:
    id: int
    title: str
    label: str
    text: str


class ChunkStore(Protocol):
    async def replace_document(
        self, source: str, doc_id: str, title: str, chunks: Sequence[tuple[Chunk, Vector]]
    ) -> None: ...

    async def nearest_chunks(
        self, vector: Vector, *, limit: int
    ) -> list[tuple[StoredChunk, float]]: ...

    async def chunks_containing(self, terms: Sequence[str], *, limit: int) -> list[StoredChunk]: ...


class NotesIndex:
    def __init__(self, *, store: ChunkStore, embedder: Embedder) -> None:
        self._store = store
        self._embedder = embedder

    async def apply(self, documents: Sequence[Document]) -> None:
        for document in documents:
            chunks: list[tuple[Chunk, Vector]] = []
            if document.text is not None:
                default_heading = PurePosixPath(document.title).stem
                for chunk in chunk_document(document.text, default_heading):
                    chunks.append((chunk, await self._embedder.embed(chunk.text)))
            await self._store.replace_document(
                document.source, document.doc_id, document.title, chunks
            )

    async def search(self, query: str) -> list[StoredChunk]:
        if not query.strip():
            return []
        nearest = await self._store.nearest_chunks(
            await self._embedder.embed(query), limit=_CANDIDATES_PER_PATH
        )
        by_vector = [chunk for chunk, similarity in nearest if similarity >= MIN_SIMILARITY]
        terms = _terms(query)
        by_keyword = (
            await self._store.chunks_containing(terms, limit=_CANDIDATES_PER_PATH) if terms else []
        )
        return _fuse(by_vector, by_keyword)[:MAX_HITS]


def _terms(query: str) -> list[str]:
    words = re.split(f"[{re.escape(_TERM_SEPARATORS)}]+", query)
    return list(dict.fromkeys(word for word in words if len(word) >= 2))


def _fuse(*rankings: Sequence[StoredChunk]) -> list[StoredChunk]:
    scores: dict[int, float] = defaultdict(float)
    chunks: dict[int, StoredChunk] = {}
    for ranking in rankings:
        for rank, chunk in enumerate(ranking, start=1):
            scores[chunk.id] += 1 / (_RRF_K + rank)
            chunks[chunk.id] = chunk
    return [chunks[chunk_id] for chunk_id in sorted(scores, key=lambda i: (-scores[i], i))]
