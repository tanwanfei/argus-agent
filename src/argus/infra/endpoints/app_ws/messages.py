from typing import Annotated, Literal, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    model_validator,
)
from pydantic_core import PydanticCustomError

_NonEmptyText = Annotated[str, Field(min_length=1)]
_TicketId = Annotated[str, Field(pattern=r"^ticket_[0-9a-f]{1,32}$")]
_Sha256Hex = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
_Base64Text = Annotated[str, Field(pattern=r"^[A-Za-z0-9+/]+={0,2}$")]


class ProtocolError(Exception):
    pass


class _WireMessage(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True, hide_input_in_errors=True)


class Hello(_WireMessage):
    t: Literal["hello"] = "hello"
    v: int = Field(ge=1)
    token: _NonEmptyText
    app_version: _NonEmptyText
    sig_expires_at: AwareDatetime


class Location(_WireMessage):
    t: Literal["location"] = "location"
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    accuracy: float = Field(ge=0)
    ts: AwareDatetime
    moving: bool


class BargeIn(_WireMessage):
    t: Literal["barge_in"] = "barge_in"
    ts: AwareDatetime


class GetTicket(_WireMessage):
    t: Literal["get_ticket"] = "get_ticket"
    id: _TicketId


class Pair(_WireMessage):
    t: Literal["pair"] = "pair"
    pubkey: _Base64Text


class Confirm(_WireMessage):
    t: Literal["confirm"] = "confirm"
    ticket_id: _TicketId
    args_hash: _Sha256Hex
    approved: bool
    sig: _Base64Text | None = None

    @model_validator(mode="after")
    def _signed_only_when_approved(self) -> Self:
        if self.approved and self.sig is None:
            raise PydanticCustomError("approved_without_sig", "批准必须带签名")
        if not self.approved and self.sig is not None:
            raise PydanticCustomError("rejected_with_sig", "拒绝不许带签名")
        return self


class End(_WireMessage):
    t: Literal["end"] = "end"
    reason: Literal["idle_timeout", "user", "phone_call"]


class Log(_WireMessage):
    t: Literal["log"] = "log"
    level: Literal["debug", "info", "warning", "error"]
    msg: _NonEmptyText


class UpText(_WireMessage):
    t: Literal["text"] = "text"
    text: _NonEmptyText


class GetHistory(_WireMessage):
    t: Literal["get_history"] = "get_history"
    before: Annotated[int, Field(ge=1)] | None = None


UpstreamMessage = Annotated[
    Hello | Location | BargeIn | GetTicket | Pair | Confirm | End | Log | UpText | GetHistory,
    Field(discriminator="t"),
]
_upstream_adapter: TypeAdapter[UpstreamMessage] = TypeAdapter(UpstreamMessage)


def decode_upstream(frame_text: str) -> UpstreamMessage:
    try:
        return _upstream_adapter.validate_json(frame_text)
    except ValidationError as error:
        raise ProtocolError(_describe(error)) from None


def _describe(error: ValidationError) -> str:
    problems = []
    for detail in error.errors(include_url=False, include_context=False, include_input=False):
        location = ".".join(str(part) for part in detail["loc"]) or "整条消息"
        problems.append(f"{location}: {detail['type']}")
    return "上行消息不合协议：" + "；".join(problems)


PROTOCOL_VERSION = 1


class Welcome(_WireMessage):
    t: Literal["welcome"] = "welcome"
    v: int = Field(ge=1)


ShownState = Literal["idle", "listening", "thinking", "speaking"]


class State(_WireMessage):
    t: Literal["state"] = "state"
    state: ShownState


class PlayBegin(_WireMessage):
    t: Literal["play_begin"] = "play_begin"


class PlayEnd(_WireMessage):
    t: Literal["play_end"] = "play_end"


class NotYou(_WireMessage):
    t: Literal["not_you"] = "not_you"


class TextEnd(_WireMessage):
    t: Literal["text_end"] = "text_end"


class ConfirmCardMessage(_WireMessage):
    t: Literal["confirm_card"] = "confirm_card"
    ticket_id: _TicketId
    title: _NonEmptyText
    lines: tuple[_NonEmptyText, ...]
    args_hash: _Sha256Hex
    expires_at: AwareDatetime


CardStateOnWire = Literal["pending", "approved", "rejected", "expired"]


class ConfirmationBlock(_WireMessage):
    ticket_id: _TicketId
    title: _NonEmptyText
    expires_at: AwareDatetime
    state: CardStateOnWire


class DownText(_WireMessage):
    t: Literal["text"] = "text"
    text: _NonEmptyText
    confirmation: ConfirmationBlock | None = None


class HistoryItem(_WireMessage):
    id: int = Field(ge=1)
    who: Literal["owner", "other", "argus"]
    text: str
    ts: AwareDatetime
    via: Literal["voice", "typed"]
    confirmation: ConfirmationBlock | None = None


class History(_WireMessage):
    t: Literal["history"] = "history"
    items: tuple[HistoryItem, ...]


class Bye(_WireMessage):
    t: Literal["bye"] = "bye"
    reason: Literal["upgrade"]


DownstreamMessage = Annotated[
    Welcome
    | State
    | PlayBegin
    | PlayEnd
    | NotYou
    | ConfirmCardMessage
    | DownText
    | TextEnd
    | History
    | Bye,
    Field(discriminator="t"),
]


def encode_downstream(message: DownstreamMessage) -> str:
    return message.model_dump_json(exclude_none=True)
