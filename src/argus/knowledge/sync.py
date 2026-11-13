import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from argus.knowledge.sources import Document, SourceConnector

logger = logging.getLogger(__name__)


class CursorStore(Protocol):
    async def cursor(self, source: str) -> str | None: ...

    async def save_cursor(self, source: str, cursor: str) -> None: ...


class DocumentSink(Protocol):
    async def apply(self, documents: Sequence[Document]) -> None: ...


@dataclass(frozen=True)
class SyncReport:
    source: str
    changed: int
    deleted: int


class KnowledgeSync:
    def __init__(
        self, *, sources: Sequence[SourceConnector], cursors: CursorStore, sink: DocumentSink
    ) -> None:
        self._sources = sources
        self._cursors = cursors
        self._sink = sink

    async def sync(self) -> list[SyncReport]:
        reports = []
        for source in self._sources:
            documents, new_cursor = await source.changes(await self._cursors.cursor(source.name))
            if documents:
                await self._sink.apply(documents)
            await self._cursors.save_cursor(source.name, new_cursor)
            deleted = sum(document.text is None for document in documents)
            report = SyncReport(source.name, changed=len(documents) - deleted, deleted=deleted)
            logger.info(
                "资料同步：%s 变了 %d 篇、删了 %d 篇", report.source, report.changed, report.deleted
            )
            reports.append(report)
        return reports
