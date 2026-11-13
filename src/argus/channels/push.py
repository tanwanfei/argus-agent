from enum import StrEnum
from typing import Protocol


class PushLevel(StrEnum):
    NORMAL = "normal"
    TIME_SENSITIVE = "time_sensitive"


class PushError(Exception):
    pass


class PushOutlet(Protocol):
    async def push(
        self, title: str, body: str, *, url: str | None = None, level: PushLevel
    ) -> None: ...
