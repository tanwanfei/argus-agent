import sqlite3
from datetime import datetime, timedelta

from argus.infra.sqlite.database import SqliteDatabase
from argus.kernel.clock import Clock
from argus.kernel.tools import CardPrompt
from argus.session.history import CardState, ChatTurn, StoredChatTurn, Via

_DELETE_EXPIRED = "DELETE FROM ses_history WHERE julianday(ts) < julianday(:expired_before)"

_INSERT_ROW = """
INSERT INTO ses_history (ts, trace_id, utterance, reply, via, card_ticket_id, card_title, card_expires_at)
VALUES (:ts, :trace_id, :utterance, :reply, :via, :card_ticket_id, :card_title, :card_expires_at)
"""

_ANSWER_CARD = "UPDATE ses_history SET card_answer = :answer WHERE card_ticket_id = :ticket_id"

_SELECT_PAGE = """
SELECT id, ts, trace_id, utterance, reply, via, card_ticket_id, card_title, card_expires_at, card_answer
FROM ses_history
WHERE julianday(ts) >= julianday(:expired_before)
  AND (:before IS NULL OR id < :before)
ORDER BY id DESC
LIMIT :limit
"""


class SqliteChatHistoryStore:
    def __init__(self, database: SqliteDatabase, clock: Clock, kept_for: timedelta) -> None:
        self._database = database
        self._clock = clock
        self._kept_for = kept_for

    async def append(self, turn: ChatTurn) -> None:
        row = {
            "ts": turn.started_at.isoformat(),
            "trace_id": turn.trace_id,
            "utterance": turn.utterance,
            "reply": turn.reply,
            "via": turn.via.value,
            "card_ticket_id": None if turn.card is None else turn.card.ticket_id,
            "card_title": None if turn.card is None else turn.card.title,
            "card_expires_at": None if turn.card is None else turn.card.expires_at.isoformat(),
        }
        cutoff = {"expired_before": self._expired_before().isoformat()}

        def expire_then_insert(connection: sqlite3.Connection) -> None:
            connection.execute(_DELETE_EXPIRED, cutoff)
            connection.execute(_INSERT_ROW, row)

        await self._database.write(expire_then_insert)

    async def answer_card(self, ticket_id: str, answer: CardState) -> None:
        parameters = {"ticket_id": ticket_id, "answer": answer.value}

        def update(connection: sqlite3.Connection) -> None:
            connection.execute(_ANSWER_CARD, parameters)

        await self._database.write(update)

    async def page(self, *, before_turn_id: int | None, limit: int) -> list[StoredChatTurn]:
        parameters = {
            "expired_before": self._expired_before().isoformat(),
            "before": before_turn_id,
            "limit": limit,
        }

        def select_page(connection: sqlite3.Connection) -> list[_Row]:
            return connection.execute(_SELECT_PAGE, parameters).fetchall()

        rows = await self._database.read(select_page)
        now = self._clock.now()
        return [_stored_turn(row, now) for row in reversed(rows)]

    def _expired_before(self) -> datetime:
        return self._clock.now() - self._kept_for


_Row = tuple[int, str, str, str, str, str, str | None, str | None, str | None, str | None]


def _stored_turn(row: _Row, now: datetime) -> StoredChatTurn:
    turn_id, ts, trace_id, utterance, reply, via, ticket_id, title, expires_at, answer = row
    card = None
    card_state = None
    if ticket_id is not None and title is not None and expires_at is not None:
        card = CardPrompt(ticket_id, title, datetime.fromisoformat(expires_at))
        card_state = _card_state(answer, card.expires_at, now)
    turn = ChatTurn(
        started_at=datetime.fromisoformat(ts),
        trace_id=trace_id,
        utterance=utterance,
        reply=reply,
        via=Via(via),
        card=card,
    )
    return StoredChatTurn(turn_id=turn_id, turn=turn, card_state=card_state)


def _card_state(answer: str | None, expires_at: datetime, now: datetime) -> CardState:
    if answer is not None:
        return CardState(answer)
    return CardState.EXPIRED if now >= expires_at else CardState.PENDING
