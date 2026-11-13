from dataclasses import dataclass

from argus.delivery.outbound import Verdict
from argus.proactive.triggers import Candidate
from argus.situation.busy import Busy


@dataclass(frozen=True)
class DecisionThresholds:
    speak_at: float
    notify_at: float
    drop_below: float
    urgent_at: int


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    urgent: bool


def decide(candidate: Candidate, weight: float, thresholds: DecisionThresholds) -> Decision:
    if candidate.asked_for:
        return Decision(Verdict.SPEAK, urgent=candidate.urgency >= thresholds.urgent_at)

    if candidate.value * weight < thresholds.drop_below:
        return Decision(Verdict.DROP, urgent=False)

    net_value = candidate.value * candidate.urgency * weight
    if net_value >= thresholds.speak_at:
        return Decision(Verdict.SPEAK, urgent=candidate.urgency >= thresholds.urgent_at)
    if net_value >= thresholds.notify_at:
        return Decision(Verdict.NOTIFY, urgent=False)
    return Decision(Verdict.DIGEST, urgent=False)


def cap_for(decision: Decision, busy: Busy) -> Decision:
    match busy:
        case Busy.ASLEEP if decision.verdict in (Verdict.SPEAK, Verdict.NOTIFY):
            return Decision(Verdict.DIGEST, urgent=False)
        case Busy.IN_MEETING if decision.verdict is Verdict.SPEAK:
            return Decision(Verdict.NOTIFY, urgent=False)
        case _:
            return decision
