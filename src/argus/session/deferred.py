from collections.abc import Callable
from contextlib import aclosing
from datetime import datetime, timedelta

from argus.cognition.brain import Brain, Sentence, TurnInput
from argus.kernel.clock import Clock
from argus.kernel.events import (
    DeferredAnswer,
    DeferredTurnAnswered,
    DeferredTurnGivenUp,
    EventBus,
)
from argus.kernel.ids import IdGen
from argus.kernel.tools import Origin, Speaker
from argus.kernel.tracing import trace
from argus.llm.gateway import CloudHealth
from argus.llm.probe import PROBE_INTERVAL_S
from argus.scheduler.scheduler import Scheduler
from argus.scheduler.store import Job
from argus.situation.summary import situation_summary

DEFERRED_TURN = "session.deferred_turn"

GIVE_UP_AFTER = timedelta(hours=24)


class DeferredTurns:
    def __init__(
        self,
        scheduler: Scheduler,
        brain: Brain,
        cloud_health: Callable[[], CloudHealth],
        bus: EventBus,
        *,
        clock: Clock,
        ids: IdGen,
    ) -> None:
        self._scheduler = scheduler
        self._brain = brain
        self._cloud_health = cloud_health
        self._bus = bus
        self._clock = clock
        self._ids = ids

    async def defer(self, utterance: str, asked_at: datetime) -> None:
        payload = {"utterance": utterance, "asked_at": asked_at.isoformat()}
        await self._scheduler.enqueue(DEFERRED_TURN, payload)

    async def run(self, job: Job) -> None:
        while self._cloud_health() is CloudHealth.DOWN:
            if self._clock.now() - _asked_at(job) > GIVE_UP_AFTER:
                await self._give_up_stale(job)
                return
            await self._clock.sleep(PROBE_INTERVAL_S)
        others = await self._others_pending(job)
        batch = [job, *others]
        answers: list[DeferredAnswer] = []
        for queued in batch:
            answers.append(DeferredAnswer(queued.payload["utterance"], await self._answer(queued)))
        self._bus.publish(
            DeferredTurnAnswered(
                ts=self._clock.now(),
                job_ids=tuple(queued.id for queued in batch),
                answers=tuple(answers),
            )
        )
        await self._drop(others)

    async def _give_up_stale(self, job: Job) -> None:
        now = self._clock.now()
        stale_others = [
            queued
            for queued in await self._others_pending(job)
            if now - _asked_at(queued) > GIVE_UP_AFTER
        ]
        stale = [job, *stale_others]
        self._bus.publish(
            DeferredTurnGivenUp(
                ts=now,
                job_ids=tuple(queued.id for queued in stale),
                utterances=tuple(queued.payload["utterance"] for queued in stale),
            )
        )
        await self._drop(stale_others)

    async def _others_pending(self, job: Job) -> list[Job]:
        return [
            queued for queued in await self._scheduler.pending(DEFERRED_TURN) if queued.id != job.id
        ]

    async def _drop(self, jobs: list[Job]) -> None:
        for done in jobs:
            await self._scheduler.cancel(done.id)

    async def _answer(self, job: Job) -> str:
        turn = TurnInput(
            job.payload["utterance"],
            history=(),
            situation=situation_summary(_asked_at(job), location=None),
            spoken=False,
            speaker=Speaker.OWNER,
            origin=Origin.USER,
            precise_location=None,
        )
        parts: list[str] = []
        with trace(self._ids.new("trace")):
            async with aclosing(self._brain.respond(turn)) as replies:
                async for event in replies:
                    if isinstance(event, Sentence):
                        parts.append(event.text)
        return "\n".join(parts)


def _asked_at(job: Job) -> datetime:
    return datetime.fromisoformat(job.payload["asked_at"])
