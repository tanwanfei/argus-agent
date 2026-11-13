from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol


@dataclass(frozen=True)
class Job:
    id: str
    kind: str
    due_at: datetime
    payload: Mapping[str, Any]
    key: str | None
    attempts: int = 0
    retry_at: datetime | None = None
    ttl: timedelta | None = None

    @property
    def run_at(self) -> datetime:
        return self.due_at if self.retry_at is None else self.retry_at


class JobStore(Protocol):
    async def add(self, job: Job) -> None: ...

    async def delete(self, job_id_or_key: str) -> None: ...

    async def earliest(self, kind: str) -> Job | None: ...

    async def pending(self, kind: str) -> list[Job]: ...

    async def retry_later(self, job_id: str, *, retry_at: datetime) -> None: ...

    async def mark_failed(self, job_id: str) -> None: ...
