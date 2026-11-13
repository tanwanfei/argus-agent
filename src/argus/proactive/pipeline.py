import logging
from datetime import datetime

from argus.delivery.delivery import Delivery
from argus.delivery.outbound import Outbound, Verdict
from argus.kernel.clock import Clock
from argus.kernel.events import Event
from argus.kernel.feedback import Feedback
from argus.kernel.ids import IdGen
from argus.proactive.decision import Decision, DecisionThresholds, cap_for, decide
from argus.proactive.dedupe import SeenKeys, drop_repeats
from argus.proactive.digest import DigestItem, DigestQueue
from argus.proactive.feedback import UselessCounts, feedback_after, weight_for
from argus.proactive.mute import MutedDays, while_muted
from argus.proactive.quota import DailyLimits, QuotaStore, spend_quota
from argus.proactive.triggers import Candidate, EnabledTriggers
from argus.proactive.usefulness import UsefulnessLog
from argus.situation.busy import BusyAt

logger = logging.getLogger(__name__)


class Proactive:
    def __init__(
        self,
        *,
        triggers: EnabledTriggers,
        seen: SeenKeys,
        quota: QuotaStore,
        useless: UselessCounts,
        usefulness: UsefulnessLog,
        digest: DigestQueue,
        muted: MutedDays,
        busy: BusyAt,
        thresholds: DecisionThresholds,
        limits: DailyLimits,
        delivery: Delivery,
        clock: Clock,
        ids: IdGen,
    ) -> None:
        self._triggers = triggers
        self._seen = seen
        self._quota = quota
        self._useless = useless
        self._usefulness = usefulness
        self._digest = digest
        self._muted = muted
        self._busy = busy
        self._thresholds = thresholds
        self._limits = limits
        self._delivery = delivery
        self._clock = clock
        self._ids = ids

    async def on_event(self, event: Event) -> None:
        now = self._clock.now()
        candidates = await drop_repeats(self._triggers.fire(event), self._seen, now)
        for candidate in candidates:
            weight = weight_for(await self._useless.count(candidate.trigger_id))
            decision = decide(candidate, weight, self._thresholds)
            if not candidate.asked_for:
                if await self._muted.is_muted(now.date()):
                    decision = while_muted(decision)
                else:
                    decision = self._capped(candidate, decision, now)
                    decision = await spend_quota(decision, self._quota, now.date(), self._limits)
            await self._send(candidate, decision, now)

    async def mark_useless(self, trigger_id: str) -> Feedback:
        await self._usefulness.record_useless(trigger_id, self._clock.now())
        useless = await self._useless.add_one(trigger_id)
        logger.info("触发器 %s 被说了第 %d 次没用", trigger_id, useless)
        return feedback_after(useless)

    def _capped(self, candidate: Candidate, decision: Decision, now: datetime) -> Decision:
        busy = self._busy(now)
        capped = cap_for(decision, busy)
        if capped.verdict is not decision.verdict:
            logger.info(
                "触发器 %s 的提醒碰上 %s，从 %s 改成 %s",
                candidate.trigger_id,
                busy.value,
                decision.verdict.value,
                capped.verdict.value,
            )
        return capped

    async def _send(self, candidate: Candidate, decision: Decision, now: datetime) -> None:
        logger.info("触发器 %s 的提醒判成 %s", candidate.trigger_id, decision.verdict.value)
        match decision.verdict:
            case Verdict.SPEAK | Verdict.NOTIFY:
                await self._delivery.announce(_outbound(candidate, decision, now))
                if not candidate.asked_for:
                    await self._usefulness.record_sent(candidate.trigger_id, decision.verdict, now)
            case Verdict.DIGEST:
                item_id = self._ids.new("digest")
                await self._digest.add(
                    DigestItem(item_id, now, candidate.trigger_id, candidate.text)
                )
            case Verdict.DROP:
                pass


def _outbound(candidate: Candidate, decision: Decision, now: datetime) -> Outbound:
    return Outbound(
        text=candidate.text,
        verdict=decision.verdict,
        urgent=decision.urgent,
        expires_at=now + candidate.ttl,
        source=candidate.trigger_id,
        personal=candidate.personal,
        asked_for=candidate.asked_for,
        on_phone_already=candidate.on_phone_already,
    )
