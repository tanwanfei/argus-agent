import asyncio
import hashlib
import json
import logging
from pathlib import Path

from argus.knowledge.sources import Document

logger = logging.getLogger(__name__)

_SUFFIXES = frozenset({".md", ".txt"})


class SelfFilesSource:
    name = "self_files"

    def __init__(self, root: Path) -> None:
        self._root = root

    async def changes(self, cursor: str | None) -> tuple[list[Document], str]:
        return await asyncio.to_thread(self._scan, cursor)

    def _scan(self, cursor: str | None) -> tuple[list[Document], str]:
        seen: dict[str, str] = {} if cursor is None else json.loads(cursor)
        current: dict[str, str] = {}
        changed: list[Document] = []
        for doc_id, path in self._files():
            raw = path.read_bytes()
            try:
                text = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                logger.warning("资料 %s 不是 UTF-8 编码，这次跳过", doc_id)
                if doc_id in seen:
                    current[doc_id] = seen[doc_id]
                continue
            digest = hashlib.sha256(raw).hexdigest()
            current[doc_id] = digest
            if seen.get(doc_id) != digest:
                changed.append(Document(self.name, doc_id, doc_id, text.replace("\r\n", "\n")))
        deleted = [
            Document(self.name, doc_id, doc_id, None)
            for doc_id in sorted(seen)
            if doc_id not in current
        ]
        return changed + deleted, json.dumps(current, ensure_ascii=False, sort_keys=True)

    def _files(self) -> list[tuple[str, Path]]:
        if not self._root.is_dir():
            logger.warning("资料目录 %s 不存在，当成没有资料", self._root)
            return []
        files = []
        for path in self._root.rglob("*"):
            relative = path.relative_to(self._root)
            if any(part.startswith((".", "~")) for part in relative.parts):
                continue
            if path.is_file() and path.suffix.lower() in _SUFFIXES:
                files.append((relative.as_posix(), path))
        return sorted(files)
