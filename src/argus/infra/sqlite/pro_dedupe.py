import sqlite3
from datetime import datetime

from argus.infra.sqlite.database import SqliteDatabase

_INSERT_IF_NEW = """
INSERT OR IGNORE INTO pro_dedupe (trigger_id, dedupe_key, seen_at) VALUES (?, ?, ?)
"""


class SqliteSeenKeys:
    def __init__(self, database: SqliteDatabase) -> None:
        self._database = database

    async def add_if_new(self, trigger_id: str, dedupe_key: str, seen_at: datetime) -> bool:
        def insert(connection: sqlite3.Connection) -> bool:
            cursor = connection.execute(
                _INSERT_IF_NEW, (trigger_id, dedupe_key, seen_at.isoformat())
            )
            return cursor.rowcount > 0

        return await self._database.write(insert)
