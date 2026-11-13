from collections.abc import Sequence

from argus.memory.records import Episode, Fact

_MIN_TERM_LENGTH = 2


def matching_facts(query: str, facts: Sequence[Fact]) -> list[Fact]:
    normalized_query = _normalize(query)
    current_facts = [fact for fact in facts if fact.superseded_by is None]
    if normalized_query:
        current_facts = [fact for fact in current_facts if _is_mentioned(normalized_query, fact)]
    return sorted(
        current_facts,
        key=lambda fact: (-fact.confidence, -fact.valid_from.timestamp(), fact.id),
    )


def _is_mentioned(normalized_query: str, fact: Fact) -> bool:
    terms = (fact.subject, fact.attribute, fact.value)
    return any(_overlaps(normalized_query, _normalize(term)) for term in terms)


def _overlaps(normalized_query: str, normalized_term: str) -> bool:
    if len(normalized_term) < _MIN_TERM_LENGTH or len(normalized_query) < _MIN_TERM_LENGTH:
        return False
    return normalized_term in normalized_query or normalized_query in normalized_term


def _normalize(text: str) -> str:
    return text.strip().casefold()


def facts_about(topic: str, facts: Sequence[Fact]) -> list[Fact]:
    normalized_topic = _check_topic(topic)
    return [
        fact
        for fact in facts
        if any(
            normalized_topic in _normalize(text)
            for text in (fact.subject, fact.attribute, fact.value)
        )
    ]


def episodes_about(topic: str, episodes: Sequence[Episode]) -> list[Episode]:
    normalized_topic = _check_topic(topic)
    return [episode for episode in episodes if normalized_topic in _normalize(episode.summary)]


def _check_topic(topic: str) -> str:
    normalized_topic = _normalize(topic)
    if len(normalized_topic) < _MIN_TERM_LENGTH:
        raise ValueError(f"要忘掉的内容太短，至少说两个字：{topic!r}")
    return normalized_topic
