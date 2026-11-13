from typing import Literal, Protocol

from argus.infra.endpoints.app_ws.messages import (
    CardStateOnWire,
    ConfirmationBlock,
    GetHistory,
    History,
    HistoryItem,
)
from argus.session.history import CardState, StoredChatTurn, Via

TURNS_PER_PAGE = 20

_VIA_ON_WIRE: dict[Via, Literal["voice", "typed"]] = {Via.VOICE: "voice", Via.TYPED: "typed"}
_CARD_STATE_ON_WIRE: dict[CardState, CardStateOnWire] = {
    CardState.PENDING: "pending",
    CardState.APPROVED: "approved",
    CardState.REJECTED: "rejected",
    CardState.EXPIRED: "expired",
}


class ChatHistoryReader(Protocol):
    async def page(self, *, before_turn_id: int | None, limit: int) -> list[StoredChatTurn]: ...


async def history_page(reader: ChatHistoryReader, request: GetHistory) -> History:
    if request.before is None:
        before_turn_id = None
    else:
        before_turn_id = (request.before + 1) // 2
    stored_turns = await reader.page(before_turn_id=before_turn_id, limit=TURNS_PER_PAGE)
    items: list[HistoryItem] = []
    for stored in stored_turns:
        items.extend(_items_of(stored))
    return History(items=tuple(items))


def _items_of(stored: StoredChatTurn) -> tuple[HistoryItem, ...]:
    turn = stored.turn
    yours = HistoryItem(
        id=2 * stored.turn_id - 1,
        who="owner",
        text=turn.utterance,
        ts=turn.started_at,
        via=_VIA_ON_WIRE[turn.via],
    )
    reply = HistoryItem(
        id=2 * stored.turn_id,
        who="argus",
        text=turn.reply,
        ts=turn.started_at,
        via=_VIA_ON_WIRE[turn.via],
        confirmation=_confirmation_of(stored),
    )
    if not turn.utterance:
        return (reply,)
    return yours, reply


def _confirmation_of(stored: StoredChatTurn) -> ConfirmationBlock | None:
    card = stored.turn.card
    if card is None or stored.card_state is None:
        return None
    return ConfirmationBlock(
        ticket_id=card.ticket_id,
        title=card.title,
        expires_at=card.expires_at,
        state=_CARD_STATE_ON_WIRE[stored.card_state],
    )
