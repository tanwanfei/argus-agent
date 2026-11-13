import sqlite3
from collections.abc import Collection
from datetime import datetime

from argus.infra.sqlite.database import SqliteDatabase
from argus.proactive.digest import DigestItem

_INSERT = "INSERT INTO pro_digest (id, queued_at, trigger_id, text) VALUES (?, ?, ?, ?)"
_SELECT_PENDING = "SELECT id, queued_at, trigger_id, text FROM pro_digest ORDER BY rowid"
_DELETE = "DELETE FROM pro_digest WHERE id = ?"


class SqliteDigestQueue:
    def __init__(self, database: SqliteDatabase) -> None:
        self._database = database

    async def add(self, item: DigestItem) -> None:
        def insert(connection: sqlite3.Connection) -> None:
            connection.execute(
                _INSERT, (item.id, item.queued_at.isoformat(), item.trigger_id, item.text)
            )

        await self._database.write(insert)

    async def pending(self) -> list[DigestItem]:
        def select_pending(connection: sqlite3.Connection) -> list[DigestItem]:
            return [
                DigestItem(item_id, datetime.fromisoformat(queued_at), trigger_id, text)
                for item_id, queued_at, trigger_id, text in connection.execute(_SELECT_PENDING)
            ]

        return await self._database.read(select_pending)

    async def remove(self, ids: Collection[str]) -> None:
        def delete(connection: sqlite3.Connection) -> None:
            connection.executemany(_DELETE, [(item_id,) for item_id in ids])

        await self._database.write(delete)
