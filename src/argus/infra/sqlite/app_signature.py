import sqlite3
from dataclasses import dataclass
from datetime import datetime

from argus.infra.sqlite.database import SqliteDatabase

_UPSERT_ROW = """
INSERT INTO app_signature (id, expires_at, app_version, reported_at)
VALUES (1, :expires_at, :app_version, :reported_at)
ON CONFLICT (id) DO UPDATE SET
    expires_at = excluded.expires_at,
    app_version = excluded.app_version,
    reported_at = excluded.reported_at
"""

_SELECT_ROW = "SELECT expires_at, app_version, reported_at FROM app_signature WHERE id = 1"


@dataclass(frozen=True)
class AppSignature:
    expires_at: datetime
    app_version: str
    reported_at: datetime


class SqliteAppSignatureStore:
    def __init__(self, database: SqliteDatabase) -> None:
        self._database = database

    async def record(
        self, *, expires_at: datetime, app_version: str, reported_at: datetime
    ) -> None:
        row = {
            "expires_at": expires_at.isoformat(),
            "app_version": app_version,
            "reported_at": reported_at.isoformat(),
        }

        def upsert(connection: sqlite3.Connection) -> None:
            connection.execute(_UPSERT_ROW, row)

        await self._database.write(upsert)

    async def latest(self) -> AppSignature | None:
        def select_row(connection: sqlite3.Connection) -> tuple[str, str, str] | None:
            return connection.execute(_SELECT_ROW).fetchone()

        row = await self._database.read(select_row)
        if row is None:
            return None
        expires_at, app_version, reported_at = row
        return AppSignature(
            expires_at=datetime.fromisoformat(expires_at),
            app_version=app_version,
            reported_at=datetime.fromisoformat(reported_at),
        )
