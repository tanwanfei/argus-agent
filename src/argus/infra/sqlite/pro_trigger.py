import sqlite3

from argus.infra.sqlite.database import SqliteDatabase

_SELECT_COUNT = "SELECT useless FROM pro_trigger WHERE trigger_id = ?"
_ADD_ONE = """
INSERT INTO pro_trigger (trigger_id, useless) VALUES (?, 1)
ON CONFLICT (trigger_id) DO UPDATE SET useless = useless + 1
RETURNING useless
"""


class SqliteUselessCounts:
    def __init__(self, database: SqliteDatabase) -> None:
        self._database = database

    async def count(self, trigger_id: str) -> int:
        def select_count(connection: sqlite3.Connection) -> int:
            row = connection.execute(_SELECT_COUNT, (trigger_id,)).fetchone()
            return 0 if row is None else int(row[0])

        return await self._database.read(select_count)

    async def add_one(self, trigger_id: str) -> int:
        def add_one(connection: sqlite3.Connection) -> int:
            (useless,) = connection.execute(_ADD_ONE, (trigger_id,)).fetchone()
            return int(useless)

        return await self._database.write(add_one)
