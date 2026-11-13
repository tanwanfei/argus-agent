from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from argus.memory.records import Fact, NewFact


class ConflictAction(StrEnum):
    ADD = "add"
    ALREADY_KNOWN = "already_known"


@dataclass(frozen=True)
class Resolution:
    action: ConflictAction
    supersedes: tuple[int, ...]


def resolve_conflict(
    new_fact: NewFact, existing_facts: Sequence[Fact], *, replaces: bool
) -> Resolution:
    same_slot = [
        fact
        for fact in existing_facts
        if fact.superseded_by is None
        and _same_text(fact.subject, new_fact.subject)
        and _same_text(fact.attribute, new_fact.attribute)
    ]
    if any(_same_text(fact.value, new_fact.value) for fact in same_slot):
        return Resolution(ConflictAction.ALREADY_KNOWN, supersedes=())
    if replaces:
        return Resolution(ConflictAction.ADD, supersedes=tuple(fact.id for fact in same_slot))
    return Resolution(ConflictAction.ADD, supersedes=())


def _same_text(first: str, second: str) -> bool:
    return first.strip().casefold() == second.strip().casefold()
