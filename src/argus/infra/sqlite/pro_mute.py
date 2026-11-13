import sqlite3
from datetime import date

from argus.infra.sqlite.database import SqliteDatabase

_INSERT = "INSERT OR IGNORE INTO pro_mute (day) VALUES (?)"
_DELETE = "DELETE FROM pro_mute WHERE day = ?"
_SELECT = "SELECT 1 FROM pro_mute WHERE day = ?"


class SqliteMutedDays:
    def __init__(self, database: SqliteDatabase) -> None:
        self._database = database

    async def mute(self, day: date) -> bool:
        def insert(connection: sqlite3.Connection) -> bool:
            return connection.execute(_INSERT, (day.isoformat(),)).rowcount > 0

        return await self._database.write(insert)

    async def unmute(self, day: date) -> bool:
        def delete(connection: sqlite3.Connection) -> bool:
            return connection.execute(_DELETE, (day.isoformat(),)).rowcount > 0

        return await self._database.write(delete)

    async def is_muted(self, day: date) -> bool:
        def select(connection: sqlite3.Connection) -> bool:
            return connection.execute(_SELECT, (day.isoformat(),)).fetchone() is not None

        return await self._database.read(select)
