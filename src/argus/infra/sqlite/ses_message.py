import sqlite3
from collections.abc import Sequence
from datetime import datetime

from argus.infra.sqlite.database import SqliteDatabase
from argus.kernel.tools import Speaker
from argus.llm.gateway import Role
from argus.session.store import (
    WorkingMessage,
    mentions_topic,
    tool_call_from_json,
    tool_call_to_json,
)

_DELETE_EXPIRED = "DELETE FROM ses_message WHERE julianday(ts) < julianday(:expired_before)"

_INSERT_ROW = """
INSERT INTO ses_message (ts, trace_id, role, speaker, content, tool_call)
VALUES (:ts, :trace_id, :role, :speaker, :content, :tool_call)
"""

_SELECT_SINCE = """
SELECT ts, trace_id, role, speaker, content, tool_call FROM ses_message
WHERE julianday(ts) >= julianday(:since)
ORDER BY id
"""


_SELECT_CONTENT = "SELECT trace_id, content || coalesce(tool_call, '') FROM ses_message"

_DELETE_TURN = "DELETE FROM ses_message WHERE trace_id = :trace_id"


class SqliteSessionStore:
    def __init__(self, database: SqliteDatabase) -> None:
        self._database = database

    async def append(self, messages: Sequence[WorkingMessage], *, expired_before: datetime) -> None:
        rows = [
            {
                "ts": message.ts.isoformat(),
                "trace_id": message.trace_id,
                "role": message.role.value,
                "speaker": message.speaker.value,
                "content": message.content,
                "tool_call": (
                    None if message.tool_call is None else tool_call_to_json(message.tool_call)
                ),
            }
            for message in messages
        ]
        cutoff = {"expired_before": expired_before.isoformat()}

        def expire_then_insert(connection: sqlite3.Connection) -> None:
            connection.execute(_DELETE_EXPIRED, cutoff)
            connection.executemany(_INSERT_ROW, rows)

        await self._database.write(expire_then_insert)

    async def forget(self, topic: str) -> None:
        def delete_turns_mentioning_topic(connection: sqlite3.Connection) -> None:
            rows = connection.execute(_SELECT_CONTENT).fetchall()
            forgotten_trace_ids = {
                trace_id for trace_id, content in rows if mentions_topic(topic, content)
            }
            for trace_id in forgotten_trace_ids:
                connection.execute(_DELETE_TURN, {"trace_id": trace_id})

        await self._database.write(delete_turns_mentioning_topic)

    async def load(self, *, since: datetime) -> list[WorkingMessage]:
        def select_since(
            connection: sqlite3.Connection,
        ) -> list[tuple[str, str, str, str, str, str | None]]:
            return connection.execute(_SELECT_SINCE, {"since": since.isoformat()}).fetchall()

        rows = await self._database.read(select_since)
        return [
            WorkingMessage(
                ts=datetime.fromisoformat(ts),
                trace_id=trace_id,
                role=Role(role),
                speaker=Speaker(speaker),
                content=content,
                tool_call=None if tool_call is None else tool_call_from_json(tool_call),
            )
            for ts, trace_id, role, speaker, content, tool_call in rows
        ]
