import hashlib
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

from argus.kernel.clock import Clock
from argus.kernel.ids import IdGen
from argus.kernel.tools import (
    Confirmation,
    ConfirmVia,
    Permission,
    Speaker,
    ToolCall,
    ToolSpec,
)

logger = logging.getLogger(__name__)

TICKET_LIFETIME = timedelta(minutes=2)

_CONFIRMED_PERMISSIONS = frozenset({Permission.L3A, Permission.L3B})


@dataclass(frozen=True)
class Ticket:
    id: str
    tool: str
    permission: Permission
    args_hash: str
    expires_at: datetime


class TicketStore(Protocol):
    async def add(self, ticket: Ticket, *, created_at: datetime) -> None: ...

    async def get(self, ticket_id: str) -> Ticket | None: ...

    async def use(self, ticket_id: str, *, now: datetime) -> bool: ...


class CardKey(Protocol):
    @property
    def public_key(self) -> ec.EllipticCurvePublicKey | None: ...


class ConfirmationRejected(Exception):
    pass


def args_hash(name: str, args: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        {"name": name, "args": args}, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _card_signed_bytes(ticket_id: str, ticket_args_hash: str) -> bytes:
    return f"{ticket_id}\n{ticket_args_hash}".encode()


class Tickets:
    def __init__(self, store: TicketStore, *, clock: Clock, ids: IdGen, card_key: CardKey) -> None:
        self._store = store
        self._clock = clock
        self._ids = ids
        self._card_key = card_key

    async def open(self, spec: ToolSpec, call: ToolCall) -> Ticket:
        if spec.permission not in _CONFIRMED_PERMISSIONS:
            raise ValueError(f"工具 {spec.name} 不是 L3，不用开确认单")
        now = self._clock.now()
        ticket = Ticket(
            id=self._ids.new("ticket"),
            tool=spec.name,
            permission=spec.permission,
            args_hash=args_hash(call.name, call.args),
            expires_at=now + TICKET_LIFETIME,
        )
        await self._store.add(ticket, created_at=now)
        return ticket

    async def redeem(self, c: Confirmation) -> Ticket:
        ticket = await self._store.get(c.ticket_id)
        if ticket is None:
            raise ConfirmationRejected("没有这张确认单")
        now = self._clock.now()
        if now >= ticket.expires_at:
            raise ConfirmationRejected("这张确认单过期了，要做的话再说一遍")
        if c.args_hash is not None and c.args_hash != ticket.args_hash:
            raise ConfirmationRejected("确认的内容和开单时的不一样")
        match c.via:
            case ConfirmVia.VOICE:
                if ticket.permission is Permission.L3B or c.speaker is not Speaker.OWNER:
                    raise ConfirmationRejected("这件事要在手机上确认")
            case ConfirmVia.CARD:
                self._check_card_signature(ticket, c.sig)
        if not await self._store.use(ticket.id, now=now):
            raise ConfirmationRejected("这张确认单已经用过了")
        return ticket

    def _check_card_signature(self, ticket: Ticket, sig: bytes | None) -> None:
        public_key = self._card_key.public_key
        if public_key is None:
            raise ConfirmationRejected("App 还没配对，认不了卡片上的确认")
        if sig is None:
            raise ConfirmationRejected("卡片上的确认没带签名")
        try:
            public_key.verify(
                sig, _card_signed_bytes(ticket.id, ticket.args_hash), ec.ECDSA(hashes.SHA256())
            )
        except InvalidSignature:
            logger.warning("确认单 %s 收到的卡片签名对不上，不认", ticket.id)
            raise ConfirmationRejected("卡片上的签名对不上") from None
