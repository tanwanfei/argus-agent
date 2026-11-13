import logging
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

from argus.kernel.events import RouteKind, TurnRecorded
from argus.kernel.tools import Speaker
from argus.memory.extractor import (
    ConversationTurn,
    ExtractionFormatError,
    Extractor,
    reply_for_extraction,
)
from argus.memory.memory import Memory
from argus.scheduler.store import Job

logger = logging.getLogger(__name__)

EXTRACT_KIND = "memory.extract"

_EXTRACTED_ROUTES = frozenset({RouteKind.CLOUD, RouteKind.DEFER})

_CONTEXT_WINDOW = timedelta(minutes=10)
_CONTEXT_MAX_TURNS = 3


@dataclass(frozen=True)
class _QueuedTurn:
    ts: datetime
    utterance: str
    reply: str


class JobEnqueuer(Protocol):
    async def enqueue(self, kind: str, payload: Mapping[str, Any]) -> str: ...


class ExtractionQueue:
    def __init__(self, *, scheduler: JobEnqueuer) -> None:
        self._scheduler = scheduler
        self._recent: deque[_QueuedTurn] = deque(maxlen=_CONTEXT_MAX_TURNS)

    async def on_turn_recorded(self, event: TurnRecorded) -> None:
        if event.speaker is not Speaker.OWNER:
            return
        if event.route not in _EXTRACTED_ROUTES:
            return
        if not event.utterance.strip():
            return
        reply = reply_for_extraction(event.reply, event.tools_called)
        context = [
            {"utterance": prior.utterance, "reply": prior.reply}
            for prior in self._recent
            if event.ts - prior.ts <= _CONTEXT_WINDOW
        ]
        self._recent.append(_QueuedTurn(event.ts, event.utterance, reply))
        await self._scheduler.enqueue(
            EXTRACT_KIND,
            {
                "ts": event.ts.isoformat(),
                "trace_id": event.trace_id,
                "utterance": event.utterance,
                "reply": reply,
                "spoken": event.spoken,
                "context": context,
            },
        )


class ExtractionWorker:
    def __init__(self, *, extractor: Extractor, memory: Memory) -> None:
        self._extractor = extractor
        self._memory = memory

    async def handle(self, job: Job) -> None:
        try:
            turn = ConversationTurn.from_payload(job.payload)
        except ValueError as error:
            logger.warning("记忆抽取任务 %s 的内容不对，丢掉：%s", job.id, error)
            return
        try:
            known = [item.text for item in await self._memory.resident()]
            extracted = await self._extractor.extract(turn, known)
        except ExtractionFormatError as error:
            logger.warning("记忆抽取任务 %s：%s，这一轮不记", job.id, error)
            return
        await self._memory.store_extracted(extracted, turn=turn)
