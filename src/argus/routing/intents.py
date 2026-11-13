import logging
import re
import string
import unicodedata
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

_SLOT_PLACEHOLDER = re.compile(r"\{(?P<slot>[A-Za-z_][A-Za-z0-9_]*)\}")


def normalize(text: str) -> str:
    folded = unicodedata.normalize("NFKC", text).lower()
    return "".join(
        char
        for char in folded
        if not unicodedata.category(char).startswith("P") and not char.isspace()
    )


def _slot_of(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    placeholder = _SLOT_PLACEHOLDER.fullmatch(value)
    return None if placeholder is None else placeholder["slot"]


_HAN_CHARACTER = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_DIGIT_RUN = re.compile(r"[0-9]+")


def _number_syllables(digits: str) -> int | None:
    if len(digits) == 1:
        return 1
    if len(digits) != 2 or digits[0] == "0":
        return None
    tens, ones = digits[0], digits[1]
    tens_syllables = 1 if tens == "1" else 2
    return tens_syllables + (0 if ones == "0" else 1)


def _syllable_counts(text: str) -> Iterator[tuple[int, int]]:
    position = 0
    total = 0
    while position < len(text):
        digits = _DIGIT_RUN.match(text, position)
        if digits is not None:
            syllables = _number_syllables(digits.group())
            if syllables is None:
                return
            position = digits.end()
        elif _HAN_CHARACTER.match(text, position):
            syllables = 1
            position += 1
        else:
            return
        total += syllables
        yield position, total


def _syllables_of(name: str) -> int | None:
    counts = list(_syllable_counts(name))
    if not counts or counts[-1][0] != len(name):
        return None
    return counts[-1][1]


def _drop_leading_syllables(text: str, syllables: int) -> str | None:
    for position, total in _syllable_counts(text):
        if total == syllables:
            return text[position:]
    return None


BUILTIN_FIELD_NAMES = frozenset({"time"})


def builtin_fields(now: datetime) -> dict[str, str]:
    return {"time": _spoken_time(now)}


def _spoken_time(now: datetime) -> str:
    hour = now.hour
    if hour < 6:
        period = "凌晨"
    elif hour < 9:
        period = "早上"
    elif hour < 12:
        period = "上午"
    elif hour < 13:
        period = "中午"
    elif hour < 18:
        period = "下午"
    else:
        period = "晚上"
    hour_in_period = hour if hour <= 12 else hour - 12
    minutes = "整" if now.minute == 0 else f" {now.minute} 分"
    return f"{period} {hour_in_period} 点{minutes}"


def _reply_fields(reply: str) -> list[str]:
    names = []
    for _, field_name, format_spec, conversion in string.Formatter().parse(reply):
        if field_name is None:
            continue
        if not field_name.isidentifier() or format_spec or conversion:
            raise ValueError(f"回复里的占位符只能写成 {{名字}}，这里是 {field_name!r}")
        names.append(field_name)
    return names


@dataclass(frozen=True)
class IntentMatch:
    intent_id: str
    tool: str | None
    args: Mapping[str, Any]
    reply: str
    ends_conversation: bool = False

    def render_reply(self, fields: Mapping[str, str]) -> str:
        return self.reply.format_map(fields)


@dataclass(frozen=True)
class IntentRule:
    id: str
    patterns: tuple[re.Pattern[str], ...]
    tool: str | None
    args: Mapping[str, Any]
    defaults: Mapping[str, Any]
    reply: str
    ends_conversation: bool = False

    def __post_init__(self) -> None:
        if not self.patterns:
            raise ValueError("至少要有一个 pattern")
        if self.tool is None and self.args:
            raise ValueError("没有 tool 就不能有 args：参数是传给工具的")
        groups_in_patterns = {name for pattern in self.patterns for name in pattern.groupindex}
        used_slots = set()
        for arg_name, value in self.args.items():
            slot = _slot_of(value)
            if slot is None:
                if isinstance(value, str) and ("{" in value or "}" in value):
                    raise ValueError(f"参数 {arg_name} 的值要么整个是 {{槽位名}}，要么不带花括号")
                continue
            if slot not in groups_in_patterns:
                raise ValueError(
                    f"参数 {arg_name} 用的槽位 {slot} 不是哪个 pattern 里的 (?P<{slot}>…)"
                )
            used_slots.add(slot)
        unused_defaults = set(self.defaults) - used_slots
        if unused_defaults:
            raise ValueError(f"defaults 里的 {sorted(unused_defaults)} 没有参数用到")
        reply_fields = _reply_fields(self.reply)
        unknown_fields = set(reply_fields) - BUILTIN_FIELD_NAMES
        if self.tool is None and unknown_fields:
            raise ValueError(
                f"没有 tool 的意图，回复里只能用 {sorted(BUILTIN_FIELD_NAMES)}，"
                f"这里有 {sorted(unknown_fields)}"
            )

    def match(self, normalized: str) -> IntentMatch | None:
        for pattern in self.patterns:
            hit = pattern.fullmatch(normalized)
            if hit is not None:
                said = {slot: text for slot, text in hit.groupdict().items() if text is not None}
                return IntentMatch(
                    self.id,
                    self.tool,
                    self._fill_args(said),
                    self.reply,
                    ends_conversation=self.ends_conversation,
                )
        return None

    def _fill_args(self, said: Mapping[str, str]) -> dict[str, Any]:
        args: dict[str, Any] = {}
        for arg_name, value in self.args.items():
            slot = _slot_of(value)
            if slot is None:
                args[arg_name] = value
            elif slot in said:
                args[arg_name] = said[slot]
            elif slot in self.defaults:
                args[arg_name] = self.defaults[slot]
        return args


@dataclass(frozen=True)
class IntentTable:
    names: tuple[str, ...]
    rules: tuple[IntentRule, ...]

    def __post_init__(self) -> None:
        seen: set[str] = set()
        for rule_id in (rule.id for rule in self.rules):
            if rule_id in seen:
                raise ValueError(f"意图 id {rule_id} 重复了")
            seen.add(rule_id)
        for name in self.names:
            if not name:
                raise ValueError("有个名字规整以后是空的：只写了标点或空白")
            if normalize(name) != name:
                raise ValueError(f"名字 {name!r} 要写成规整后的样子：不带标点、空白，英文小写")

    @classmethod
    def build(cls, names: Sequence[str], rules: Sequence[IntentRule]) -> "IntentTable":
        return cls(tuple(normalize(name) for name in names), tuple(rules))

    def command_of(self, text: str, *, heard_wake_word: bool = False) -> str:
        return self._without_name(normalize(text), heard_wake_word)

    def match(self, text: str, *, heard_wake_word: bool = False) -> IntentMatch | None:
        command = self.command_of(text, heard_wake_word=heard_wake_word)
        if not command:
            return None
        hits = [hit for rule in self.rules if (hit := rule.match(command)) is not None]
        if len(hits) > 1:
            logger.warning("一句话对上了好几条意图，交给云端：%s", [hit.intent_id for hit in hits])
            return None
        return hits[0] if hits else None

    def _without_name(self, normalized: str, heard_wake_word: bool) -> str:
        for name in sorted(self.names, key=len, reverse=True):
            if normalized.startswith(name):
                return normalized.removeprefix(name)
        if heard_wake_word and self.names:
            name_syllables = _syllables_of(self.names[0])
            if name_syllables is not None:
                rest = _drop_leading_syllables(normalized, name_syllables)
                if rest is not None:
                    return rest
        return normalized
