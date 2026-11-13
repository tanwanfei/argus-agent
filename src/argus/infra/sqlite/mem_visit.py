import sqlite3
from datetime import datetime

from argus.infra.sqlite.database import SqliteDatabase
from argus.memory.visits import Visit

_INSERT = "INSERT INTO mem_visit (place, arrived, at) VALUES (?, ?, ?)"
_SELECT_SINCE = """
SELECT place, arrived, at FROM mem_visit
WHERE julianday(at) >= julianday(?)
ORDER BY julianday(at), id
"""
_DELETE_BEFORE = "DELETE FROM mem_visit WHERE julianday(at) < julianday(?)"


class SqliteVisitLog:
    def __init__(self, database: SqliteDatabase) -> None:
        self._database = database

    async def add(self, visit: Visit) -> None:
        row = (visit.place, int(visit.arrived), visit.at.isoformat())

        def insert(connection: sqlite3.Connection) -> None:
            connection.execute(_INSERT, row)

        await self._database.write(insert)

    async def since(self, start: datetime) -> list[Visit]:
        def select_since(connection: sqlite3.Connection) -> list[Visit]:
            return [
                Visit(place=place, arrived=bool(arrived), at=datetime.fromisoformat(at))
                for place, arrived, at in connection.execute(_SELECT_SINCE, (start.isoformat(),))
            ]

        return await self._database.read(select_since)

    async def delete_before(self, cutoff: datetime) -> int:
        def delete(connection: sqlite3.Connection) -> int:
            return connection.execute(_DELETE_BEFORE, (cutoff.isoformat(),)).rowcount

        return await self._database.write(delete)
