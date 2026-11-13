import sqlite3
from dataclasses import dataclass
from datetime import datetime

from argus.infra.sqlite.database import SqliteDatabase
from argus.kernel.events import TurnCompleted

_INSERT_ROW = """
INSERT INTO obs_turn (
    ts, trace_id, endpoint,
    asr_final_ms, route_done_ms, llm_first_token_ms, tts_first_chunk_ms, play_start_ms, route
) VALUES (
    :ts, :trace_id, :endpoint,
    :asr_final_ms, :route_done_ms, :llm_first_token_ms, :tts_first_chunk_ms, :play_start_ms, :route
)
"""


@dataclass(frozen=True)
class TurnLatency:
    ts: datetime
    trace_id: str
    endpoint: str
    asr_final_ms: int | None
    route_done_ms: int | None
    llm_first_token_ms: int | None
    tts_first_chunk_ms: int | None
    play_start_ms: int | None
    route: str | None = None


class SqliteTurnLog:
    def __init__(self, database: SqliteDatabase) -> None:
        self._database = database

    async def record(self, turn: TurnLatency) -> None:
        row = {
            "ts": turn.ts.isoformat(),
            "trace_id": turn.trace_id,
            "endpoint": turn.endpoint,
            "asr_final_ms": turn.asr_final_ms,
            "route_done_ms": turn.route_done_ms,
            "llm_first_token_ms": turn.llm_first_token_ms,
            "tts_first_chunk_ms": turn.tts_first_chunk_ms,
            "play_start_ms": turn.play_start_ms,
            "route": turn.route,
        }

        def insert_row(connection: sqlite3.Connection) -> None:
            connection.execute(_INSERT_ROW, row)

        await self._database.write(insert_row)

    async def on_turn_completed(self, event: TurnCompleted) -> None:
        if event.trace_id is None:
            raise ValueError("TurnCompleted 没带 trace_id，obs_turn 这一行对不上 llm_cost")
        await self.record(
            TurnLatency(
                ts=event.ts,
                trace_id=event.trace_id,
                endpoint=event.endpoint_id,
                asr_final_ms=event.asr_final_ms,
                route_done_ms=event.route_done_ms,
                llm_first_token_ms=event.llm_first_token_ms,
                tts_first_chunk_ms=event.tts_first_chunk_ms,
                play_start_ms=event.play_start_ms,
                route=event.route,
            )
        )
