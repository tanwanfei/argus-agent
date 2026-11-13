from typing import Protocol

from argus.kernel.feedback import Feedback

USELESS_LIMIT = 3
_WEIGHT_STEP = 0.8


class UselessCounts(Protocol):
    async def count(self, trigger_id: str) -> int: ...

    async def add_one(self, trigger_id: str) -> int: ...


def weight_for(useless: int) -> float:
    if useless >= USELESS_LIMIT:
        return 0.0
    return _WEIGHT_STEP**useless


def feedback_after(useless: int) -> Feedback:
    return Feedback.STOPPED if useless >= USELESS_LIMIT else Feedback.LOWERED
