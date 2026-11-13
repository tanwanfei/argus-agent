import sqlite3
from datetime import datetime

from argus.delivery.outbound import Verdict
from argus.infra.sqlite.database import SqliteDatabase

_INSERT_SENT = "INSERT INTO pro_sent (sent_at, trigger_id, verdict) VALUES (?, ?, ?)"
_INSERT_USELESS = "INSERT INTO pro_useless_said (said_at, trigger_id) VALUES (?, ?)"


class SqliteUsefulnessLog:
    def __init__(self, database: SqliteDatabase) -> None:
        self._database = database

    async def record_sent(self, trigger_id: str, verdict: Verdict, at: datetime) -> None:
        def insert(connection: sqlite3.Connection) -> None:
            connection.execute(_INSERT_SENT, (at.isoformat(), trigger_id, verdict.value))

        await self._database.write(insert)

    async def record_useless(self, trigger_id: str, at: datetime) -> None:
        def insert(connection: sqlite3.Connection) -> None:
            connection.execute(_INSERT_USELESS, (at.isoformat(), trigger_id))

        await self._database.write(insert)
