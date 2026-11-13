from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from argus.kernel.tools import CardPrompt


class Via(StrEnum):
    VOICE = "voice"
    TYPED = "typed"


@dataclass(frozen=True)
class ChatTurn:
    started_at: datetime
    trace_id: str
    utterance: str
    reply: str
    via: Via
    card: CardPrompt | None = None


class CardState(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


@dataclass(frozen=True)
class StoredChatTurn:
    turn_id: int
    turn: ChatTurn
    card_state: CardState | None = None


class ChatHistoryStore(Protocol):
    async def append(self, turn: ChatTurn) -> None: ...

    async def answer_card(self, ticket_id: str, answer: CardState) -> None: ...
