import logging
from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from argus.proactive.triggers import Candidate

logger = logging.getLogger(__name__)


class SeenKeys(Protocol):
    async def add_if_new(self, trigger_id: str, dedupe_key: str, seen_at: datetime) -> bool: ...


async def drop_repeats(
    candidates: Sequence[Candidate], seen: SeenKeys, now: datetime
) -> list[Candidate]:
    fresh = []
    for candidate in candidates:
        if await seen.add_if_new(candidate.trigger_id, candidate.dedupe_key, now):
            fresh.append(candidate)
        else:
            logger.info("触发器 %s 的这条提醒过了，不再提醒", candidate.trigger_id)
    return fresh
