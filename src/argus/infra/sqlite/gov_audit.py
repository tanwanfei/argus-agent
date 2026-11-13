import json
import sqlite3
from typing import assert_never

from argus.governance.governance import (
    Allow,
    AllowWithUndo,
    AuditEntry,
    ConfirmByCard,
    ConfirmByVoice,
    Decision,
    Deny,
)
from argus.infra.sqlite.database import SqliteDatabase

_INSERT_ROW = """
INSERT INTO gov_audit (
    ts, trace_id, call_id, origin, speaker, tool,
    args_json, decision, deny_reason, result, duration_ms, confirmed_via
) VALUES (
    :ts, :trace_id, :call_id, :origin, :speaker, :tool,
    :args_json, :decision, :deny_reason, :result, :duration_ms, :confirmed_via
)
"""


class SqliteAuditLog:
    def __init__(self, database: SqliteDatabase) -> None:
        self._database = database

    async def append(self, entry: AuditEntry) -> None:
        decision, deny_reason = _decision_columns(entry.decision)
        row = {
            "ts": entry.ts.isoformat(),
            "trace_id": entry.trace_id,
            "call_id": entry.call_id,
            "origin": entry.origin.value,
            "speaker": entry.speaker.value,
            "tool": entry.tool,
            "args_json": json.dumps(dict(entry.args), ensure_ascii=False),
            "decision": decision,
            "deny_reason": deny_reason,
            "result": None if entry.result is None else entry.result.value,
            "duration_ms": entry.duration_ms,
            "confirmed_via": None if entry.confirmed_via is None else entry.confirmed_via.value,
        }

        def insert_row(connection: sqlite3.Connection) -> None:
            connection.execute(_INSERT_ROW, row)

        await self._database.write(insert_row)


def _decision_columns(decision: Decision) -> tuple[str, str | None]:
    match decision:
        case Allow():
            return "allow", None
        case AllowWithUndo():
            return "allow_with_undo", None
        case ConfirmByVoice():
            return "confirm_by_voice", None
        case ConfirmByCard():
            return "confirm_by_card", None
        case Deny(reason=reason):
            return "deny", reason
        case _:
            assert_never(decision)
