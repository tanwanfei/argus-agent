from datetime import timedelta

from pydantic import BaseModel, ConfigDict

from argus.kernel.events import DeferredTurnAnswered, DeferredTurnGivenUp
from argus.proactive.triggers import Candidate, Trigger, trigger

_REPLY_CHARS = 300
_BODY_CHARS = 1200
_TTL = timedelta(hours=1)


class _NoParams(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


def _answered_text(event: DeferredTurnAnswered) -> str:
    if len(event.answers) == 1:
        (only,) = event.answers
        return f"先记下的事办好了。你问：{only.utterance}\n{_cut(only.reply, _REPLY_CHARS)}"
    lines = [f"先记下的 {len(event.answers)} 件事办好了。"]
    for number, answer in enumerate(event.answers, start=1):
        lines.append(f"{number}. 你问：{answer.utterance}\n{_cut(answer.reply, _REPLY_CHARS)}")
    return "\n".join(lines)


def _given_up_text(event: DeferredTurnGivenUp) -> str:
    if len(event.utterances) == 1:
        (only,) = event.utterances
        return f"先记下的事没办成。你问的是：{only}\n过了一天还连不上，就不补了。"
    lines = [f"先记下的 {len(event.utterances)} 件事没办成，过了一天还连不上，就不补了。你问的是："]
    lines += [
        f"{number}. {utterance}" for number, utterance in enumerate(event.utterances, start=1)
    ]
    return "\n".join(lines)


@trigger(on=DeferredTurnAnswered, id="deferred_turn_answered", params=_NoParams)
def deferred_turn_answered(event: DeferredTurnAnswered, params: _NoParams) -> Candidate:
    return Candidate(
        trigger_id="deferred_turn_answered",
        text=_cut(_answered_text(event), _BODY_CHARS),
        value=7,
        urgency=4,
        ttl=_TTL,
        dedupe_key=",".join(event.job_ids),
    )


@trigger(on=DeferredTurnGivenUp, id="deferred_turn_given_up", params=_NoParams)
def deferred_turn_given_up(event: DeferredTurnGivenUp, params: _NoParams) -> Candidate:
    return Candidate(
        trigger_id="deferred_turn_given_up",
        text=_cut(_given_up_text(event), _BODY_CHARS),
        value=6,
        urgency=4,
        ttl=_TTL,
        dedupe_key=",".join(event.job_ids),
    )


DEFERRED_ANSWER_TRIGGERS: tuple[Trigger, ...] = (deferred_turn_answered, deferred_turn_given_up)
