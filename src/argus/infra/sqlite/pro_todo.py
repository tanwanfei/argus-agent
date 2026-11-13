import sqlite3
from datetime import datetime

from argus.infra.sqlite.database import SqliteDatabase
from argus.proactive.todo import TodoItem

_INSERT = "INSERT INTO pro_todo (title, created_at) VALUES (?, ?)"

_SELECT_PENDING = """
SELECT id, title, created_at FROM pro_todo WHERE done_at IS NULL
ORDER BY julianday(created_at), id
"""

_MARK_DONE = "UPDATE pro_todo SET done_at = ? WHERE id = ? AND done_at IS NULL"
_REOPEN = "UPDATE pro_todo SET done_at = NULL WHERE id = ? AND done_at IS NOT NULL"
_DELETE = "DELETE FROM pro_todo WHERE id = ?"


class SqliteTodoList:
    def __init__(self, database: SqliteDatabase) -> None:
        self._database = database

    async def add(self, title: str, created_at: datetime) -> TodoItem:
        def insert(connection: sqlite3.Connection) -> int:
            cursor = connection.execute(_INSERT, (title, created_at.isoformat()))
            assert cursor.lastrowid is not None
            return cursor.lastrowid

        item_id = await self._database.write(insert)
        return TodoItem(item_id, title, created_at)

    async def pending(self) -> list[TodoItem]:
        def select_pending(connection: sqlite3.Connection) -> list[tuple[int, str, str]]:
            return connection.execute(_SELECT_PENDING).fetchall()

        rows = await self._database.read(select_pending)
        return [
            TodoItem(item_id, title, datetime.fromisoformat(created_at))
            for item_id, title, created_at in rows
        ]

    async def mark_done(self, item_id: int, done_at: datetime) -> bool:
        def update(connection: sqlite3.Connection) -> bool:
            return connection.execute(_MARK_DONE, (done_at.isoformat(), item_id)).rowcount > 0

        return await self._database.write(update)

    async def reopen(self, item_id: int) -> bool:
        def update(connection: sqlite3.Connection) -> bool:
            return connection.execute(_REOPEN, (item_id,)).rowcount > 0

        return await self._database.write(update)

    async def remove(self, item_id: int) -> bool:
        def delete(connection: sqlite3.Connection) -> bool:
            return connection.execute(_DELETE, (item_id,)).rowcount > 0

        return await self._database.write(delete)
