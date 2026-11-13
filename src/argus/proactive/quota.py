from dataclasses import dataclass
from datetime import date
from typing import Protocol

from argus.delivery.outbound import Verdict
from argus.proactive.decision import Decision


@dataclass(frozen=True)
class DailyLimits:
    speak_per_day: int
    notify_per_day: int


class QuotaStore(Protocol):
    async def try_use(self, day: date, verdict: Verdict, limit: int) -> bool: ...


async def spend_quota(
    decision: Decision, store: QuotaStore, today: date, limits: DailyLimits
) -> Decision:
    if decision.verdict is Verdict.SPEAK:
        if await store.try_use(today, Verdict.SPEAK, limits.speak_per_day):
            return decision
        decision = Decision(Verdict.NOTIFY, urgent=False)

    if decision.verdict is Verdict.NOTIFY:
        if await store.try_use(today, Verdict.NOTIFY, limits.notify_per_day):
            return decision
        return Decision(Verdict.DIGEST, urgent=False)

    return decision
