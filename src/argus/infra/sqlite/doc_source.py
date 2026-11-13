import sqlite3

from argus.infra.sqlite.database import SqliteDatabase

_UPSERT_CURSOR = """
INSERT INTO doc_source (name, cursor) VALUES (:name, :cursor)
ON CONFLICT (name) DO UPDATE SET cursor = excluded.cursor
"""

_SELECT_CURSOR = "SELECT cursor FROM doc_source WHERE name = ?"


class SqliteCursorStore:
    def __init__(self, database: SqliteDatabase) -> None:
        self._database = database

    async def cursor(self, source: str) -> str | None:
        def select_cursor(connection: sqlite3.Connection) -> str | None:
            row = connection.execute(_SELECT_CURSOR, (source,)).fetchone()
            return None if row is None else str(row[0])

        return await self._database.read(select_cursor)

    async def save_cursor(self, source: str, cursor: str) -> None:
        def upsert(connection: sqlite3.Connection) -> None:
            connection.execute(_UPSERT_CURSOR, {"name": source, "cursor": cursor})

        await self._database.write(upsert)
