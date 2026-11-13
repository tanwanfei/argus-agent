import math
from collections.abc import Sequence

from argus.memory.records import MemoryItem

DEFAULT_MAX_TOKENS = 2000

_PER_ITEM_OVERHEAD_TOKENS = 1


def estimate_tokens(text: str) -> int:
    ascii_count = sum(1 for character in text if character.isascii())
    other_count = len(text) - ascii_count
    return other_count + math.ceil(ascii_count / 3)


def merge_recalled(
    *,
    facts: Sequence[MemoryItem],
    habits: Sequence[MemoryItem],
    episodes: Sequence[MemoryItem],
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> list[MemoryItem]:
    if max_tokens < 0:
        raise ValueError(f"token 上限不能是负数，收到 {max_tokens}")
    merged: list[MemoryItem] = []
    seen_texts: set[str] = set()
    tokens_used = 0
    for item in (*facts, *habits, *episodes):
        normalized_text = _normalize(item.text)
        if normalized_text in seen_texts:
            continue
        cost = estimate_tokens(item.text) + _PER_ITEM_OVERHEAD_TOKENS
        if tokens_used + cost > max_tokens:
            break
        seen_texts.add(normalized_text)
        tokens_used += cost
        merged.append(item)
    return merged


def _normalize(text: str) -> str:
    return " ".join(text.split()).casefold()
