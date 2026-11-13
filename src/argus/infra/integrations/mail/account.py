from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class MailSummary:
    uid: int
    sender: str
    subject: str
    sent_at: datetime | None
    unread: bool


@dataclass(frozen=True)
class MailPage:
    messages: list[MailSummary]
    total: int


@dataclass(frozen=True)
class MailMessage:
    summary: MailSummary
    recipients: str
    body: str
    body_truncated: bool
    attachments: tuple[str, ...]


@dataclass(frozen=True)
class OutgoingMail:
    to: tuple[str, ...]
    subject: str
    body: str


class MailAccount(Protocol):
    async def recent(self, *, unread_only: bool, limit: int) -> MailPage: ...

    async def read(self, uid: int) -> MailMessage: ...

    async def save_draft(
        self, draft: OutgoingMail, *, message_id: str, created_at: datetime
    ) -> None: ...

    async def delete_draft(self, message_id: str) -> bool: ...


class MailSender(Protocol):
    async def send(self, mail: OutgoingMail, *, message_id: str, created_at: datetime) -> None: ...
