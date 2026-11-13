import sqlite3
from datetime import date

from argus.delivery.outbound import Verdict
from argus.infra.sqlite.database import SqliteDatabase

_START_DAY = "INSERT OR IGNORE INTO pro_quota (day, verdict, used) VALUES (?, ?, 0)"
_USE_ONE = "UPDATE pro_quota SET used = used + 1 WHERE day = ? AND verdict = ? AND used < ?"


class SqliteQuotaStore:
    def __init__(self, database: SqliteDatabase) -> None:
        self._database = database

    async def try_use(self, day: date, verdict: Verdict, limit: int) -> bool:
        def use_one(connection: sqlite3.Connection) -> bool:
            connection.execute(_START_DAY, (day.isoformat(), verdict.value))
            cursor = connection.execute(_USE_ONE, (day.isoformat(), verdict.value, limit))
            return cursor.rowcount > 0

        return await self._database.write(use_one)
