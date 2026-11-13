from typing import Protocol


class IdGen(Protocol):
    def new(self, kind: str) -> str: ...
