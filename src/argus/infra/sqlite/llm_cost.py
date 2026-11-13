import sqlite3
from datetime import datetime

from argus.infra.sqlite.database import SqliteDatabase
from argus.llm.budgeted import CostEntry

_INSERT_ROW = """
INSERT INTO llm_cost (
    ts, trace_id, purpose, tier, model,
    input_tokens, cached_input_tokens, output_tokens, cost_yuan, estimated
) VALUES (
    :ts, :trace_id, :purpose, :tier, :model,
    :input_tokens, :cached_input_tokens, :output_tokens, :cost_yuan, :estimated
)
"""

_SUM_COST_SINCE = """
SELECT COALESCE(SUM(cost_yuan), 0.0) FROM llm_cost WHERE julianday(ts) >= julianday(:start)
"""


_EXTRA_BUDGET = "SELECT extra_yuan FROM llm_budget_extra WHERE month = :month"

_ADD_EXTRA_BUDGET = """
INSERT INTO llm_budget_extra (month, extra_yuan, updated_at) VALUES (:month, :yuan, :at)
ON CONFLICT (month) DO UPDATE SET
    extra_yuan = extra_yuan + excluded.extra_yuan,
    updated_at = excluded.updated_at
"""


class SqliteCostLedger:
    def __init__(self, database: SqliteDatabase) -> None:
        self._database = database

    async def record(self, entry: CostEntry) -> None:
        row = {
            "ts": entry.ts.isoformat(),
            "trace_id": entry.trace_id,
            "purpose": entry.purpose.value,
            "tier": entry.tier.value,
            "model": entry.usage.model,
            "input_tokens": entry.usage.input_tokens,
            "cached_input_tokens": entry.usage.cached_input_tokens,
            "output_tokens": entry.usage.output_tokens,
            "cost_yuan": entry.cost_yuan,
            "estimated": int(entry.estimated),
        }

        def insert_row(connection: sqlite3.Connection) -> None:
            connection.execute(_INSERT_ROW, row)

        await self._database.write(insert_row)

    async def spent_since(self, start: datetime) -> float:
        start_text = start.isoformat()

        def sum_cost(connection: sqlite3.Connection) -> float:
            (total_yuan,) = connection.execute(_SUM_COST_SINCE, {"start": start_text}).fetchone()
            return float(total_yuan)

        return await self._database.read(sum_cost)

    async def extra_budget_yuan(self, month: str) -> float:
        def read_extra(connection: sqlite3.Connection) -> float:
            row = connection.execute(_EXTRA_BUDGET, {"month": month}).fetchone()
            return 0.0 if row is None else float(row[0])

        return await self._database.read(read_extra)

    async def add_extra_budget(self, month: str, yuan: float, at: datetime) -> None:
        row = {"month": month, "yuan": yuan, "at": at.isoformat()}

        def add(connection: sqlite3.Connection) -> None:
            connection.execute(_ADD_EXTRA_BUDGET, row)

        await self._database.write(add)
