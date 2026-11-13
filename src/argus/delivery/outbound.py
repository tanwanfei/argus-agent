from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

PUSH_TITLE = "提醒"


class Verdict(StrEnum):
    SPEAK = "speak"
    NOTIFY = "notify"
    DIGEST = "digest"
    DROP = "drop"


@dataclass(frozen=True)
class Outbound:
    text: str
    verdict: Verdict
    urgent: bool
    expires_at: datetime
    source: str
    personal: bool
    asked_for: bool = False
    on_phone_already: bool = False


@dataclass(frozen=True)
class Delivered:
    at: datetime
    message: Outbound
