from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Document:
    source: str
    doc_id: str
    title: str
    text: str | None


class SourceConnector(Protocol):
    name: str

    async def changes(self, cursor: str | None) -> tuple[list[Document], str]: ...
