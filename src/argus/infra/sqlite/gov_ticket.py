import sqlite3
from datetime import datetime
from typing import Any

from argus.governance.tickets import Ticket
from argus.infra.sqlite.database import SqliteDatabase
from argus.kernel.tools import Permission

_INSERT_TICKET = """
INSERT INTO gov_ticket (id, tool, permission, args_hash, created_at, expires_at)
VALUES (:id, :tool, :permission, :args_hash, :created_at, :expires_at)
"""

_SELECT_TICKET = """
SELECT id, tool, permission, args_hash, expires_at FROM gov_ticket WHERE id = :id
"""

_USE_TICKET = """
UPDATE gov_ticket SET used_at = :now
WHERE id = :id AND used_at IS NULL AND julianday(expires_at) > julianday(:now)
"""


class SqliteTicketStore:
    def __init__(self, database: SqliteDatabase) -> None:
        self._database = database

    async def add(self, ticket: Ticket, *, created_at: datetime) -> None:
        row = {
            "id": ticket.id,
            "tool": ticket.tool,
            "permission": ticket.permission.value,
            "args_hash": ticket.args_hash,
            "created_at": created_at.isoformat(),
            "expires_at": ticket.expires_at.isoformat(),
        }

        def insert(connection: sqlite3.Connection) -> None:
            connection.execute(_INSERT_TICKET, row)

        await self._database.write(insert)

    async def get(self, ticket_id: str) -> Ticket | None:
        def select(connection: sqlite3.Connection) -> tuple[Any, ...] | None:
            return connection.execute(_SELECT_TICKET, {"id": ticket_id}).fetchone()

        row = await self._database.read(select)
        if row is None:
            return None
        found_id, tool, permission, hashed_args, expires_at = row
        return Ticket(
            id=found_id,
            tool=tool,
            permission=Permission(permission),
            args_hash=hashed_args,
            expires_at=datetime.fromisoformat(expires_at),
        )

    async def use(self, ticket_id: str, *, now: datetime) -> bool:
        def mark_used(connection: sqlite3.Connection) -> bool:
            cursor = connection.execute(_USE_TICKET, {"id": ticket_id, "now": now.isoformat()})
            return cursor.rowcount == 1

        return await self._database.write(mark_used)
