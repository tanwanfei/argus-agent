import json
import sqlite3
from datetime import datetime, timedelta
from typing import Any

from argus.infra.sqlite.database import SqliteDatabase
from argus.scheduler.store import Job

_DELETE_PENDING_WITH_KEY = "DELETE FROM sch_job WHERE key = :key AND status = 'pending'"

_INSERT_JOB = """
INSERT INTO sch_job (id, kind, key, due_at, payload_json, status, attempts, retry_at, ttl_s)
VALUES (:id, :kind, :key, :due_at, :payload_json, 'pending', :attempts, :retry_at, :ttl_s)
"""

_DELETE_BY_ID_OR_KEY = "DELETE FROM sch_job WHERE id = :id_or_key OR key = :id_or_key"

_SELECT_PENDING = """
SELECT id, kind, key, due_at, payload_json, attempts, retry_at, ttl_s FROM sch_job
WHERE kind = :kind AND status = 'pending'
ORDER BY julianday(COALESCE(retry_at, due_at)), rowid
"""
_SELECT_EARLIEST = _SELECT_PENDING + "LIMIT 1"

_RETRY_LATER = """
UPDATE sch_job SET attempts = attempts + 1, retry_at = :retry_at
WHERE id = :id AND status = 'pending'
"""

_MARK_FAILED = "UPDATE sch_job SET status = 'failed' WHERE id = :id"


class SqliteJobStore:
    def __init__(self, database: SqliteDatabase) -> None:
        self._database = database

    async def add(self, job: Job) -> None:
        row = {
            "id": job.id,
            "kind": job.kind,
            "key": job.key,
            "due_at": job.due_at.isoformat(),
            "payload_json": json.dumps(job.payload, ensure_ascii=False),
            "attempts": job.attempts,
            "retry_at": None if job.retry_at is None else job.retry_at.isoformat(),
            "ttl_s": None if job.ttl is None else job.ttl.total_seconds(),
        }

        def replace_then_insert(connection: sqlite3.Connection) -> None:
            if job.key is not None:
                connection.execute(_DELETE_PENDING_WITH_KEY, {"key": job.key})
            connection.execute(_INSERT_JOB, row)

        await self._database.write(replace_then_insert)

    async def delete(self, job_id_or_key: str) -> None:
        def delete_matching(connection: sqlite3.Connection) -> None:
            connection.execute(_DELETE_BY_ID_OR_KEY, {"id_or_key": job_id_or_key})

        await self._database.write(delete_matching)

    async def earliest(self, kind: str) -> Job | None:
        def select_earliest(connection: sqlite3.Connection) -> tuple[Any, ...] | None:
            return connection.execute(_SELECT_EARLIEST, {"kind": kind}).fetchone()

        row = await self._database.read(select_earliest)
        return None if row is None else _job_from_row(row)

    async def pending(self, kind: str) -> list[Job]:
        def select_pending(connection: sqlite3.Connection) -> list[tuple[Any, ...]]:
            return connection.execute(_SELECT_PENDING, {"kind": kind}).fetchall()

        return [_job_from_row(row) for row in await self._database.read(select_pending)]

    async def retry_later(self, job_id: str, *, retry_at: datetime) -> None:
        def postpone(connection: sqlite3.Connection) -> None:
            connection.execute(_RETRY_LATER, {"id": job_id, "retry_at": retry_at.isoformat()})

        await self._database.write(postpone)

    async def mark_failed(self, job_id: str) -> None:
        def mark(connection: sqlite3.Connection) -> None:
            connection.execute(_MARK_FAILED, {"id": job_id})

        await self._database.write(mark)


def _job_from_row(row: tuple[Any, ...]) -> Job:
    job_id, job_kind, key, due_at, payload_json, attempts, retry_at, ttl_s = row
    return Job(
        id=job_id,
        kind=job_kind,
        due_at=datetime.fromisoformat(due_at),
        payload=json.loads(payload_json),
        key=key,
        attempts=attempts,
        retry_at=None if retry_at is None else datetime.fromisoformat(retry_at),
        ttl=None if ttl_s is None else timedelta(seconds=ttl_s),
    )
