from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class DigestItem:
    id: str
    queued_at: datetime
    trigger_id: str
    text: str


class DigestQueue(Protocol):
    async def add(self, item: DigestItem) -> None: ...

    async def pending(self) -> list[DigestItem]: ...

    async def remove(self, ids: Collection[str]) -> None: ...
