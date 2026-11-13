from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from argus.channels.audio import AudioChunk, AudioFrame
from argus.kernel.dialog import DialogState
from argus.kernel.tools import CardPrompt


@dataclass(frozen=True, kw_only=True)
class Capabilities:
    audio_in: bool
    audio_out: bool
    text: bool
    confirm_card: bool
    location: bool


class EndpointOffline(Exception):
    pass


@dataclass(frozen=True)
class ConfirmCard:
    ticket_id: str
    title: str
    lines: tuple[str, ...]
    args_hash: str
    expires_at: datetime


class Endpoint(Protocol):
    @property
    def id(self) -> str: ...

    @property
    def caps(self) -> Capabilities: ...

    def is_online(self) -> bool: ...

    def frames(self) -> AsyncGenerator[AudioFrame]: ...

    async def play(self, chunks: AsyncIterator[AudioChunk]) -> None: ...

    async def stop(self) -> None: ...

    def played_ms(self) -> float: ...

    async def send_text(self, text: str, *, card: CardPrompt | None = None) -> None: ...

    def end_text(self) -> None: ...

    async def show_card(self, card: ConfirmCard) -> None: ...

    def show_state(self, state: DialogState) -> None: ...

    def show_not_you(self) -> None: ...
