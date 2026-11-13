from collections.abc import Sequence
from dataclasses import dataclass

from argus.memory.records import Fact, FactSource

_BULLET_PREFIXES = ("- ", "* ")
_COLONS = ("：", ":")
_VALUE_SEPARATOR = "、"


@dataclass(frozen=True)
class ProfileEntry:
    subject: str
    attribute: str
    value: str


@dataclass(frozen=True)
class ProfileError:
    line_number: int
    message: str


@dataclass(frozen=True)
class ParsedProfile:
    entries: tuple[ProfileEntry, ...]
    errors: tuple[ProfileError, ...]


def parse_profile(text: str) -> ParsedProfile:
    entries: list[ProfileEntry] = []
    errors: list[ProfileError] = []
    subject: str | None = None
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if line == "##" or line.startswith("## "):
            subject = line[2:].strip()
            if not subject:
                errors.append(
                    ProfileError(line_number, "二级标题是空的，标题要写主体（我、妈妈……）")
                )
                subject = None
        elif line.startswith(_BULLET_PREFIXES):
            _parse_bullet(line[2:].strip(), line_number, subject, entries, errors)
    unique_entries = tuple(dict.fromkeys(entries))
    return ParsedProfile(entries=unique_entries, errors=tuple(errors))


def _parse_bullet(
    content: str,
    line_number: int,
    subject: str | None,
    entries: list[ProfileEntry],
    errors: list[ProfileError],
) -> None:
    if subject is None:
        errors.append(ProfileError(line_number, "这一条前面没有 ## 主体 标题，不知道说的是谁"))
        return
    separator_at = min((content.find(colon) for colon in _COLONS if colon in content), default=-1)
    if separator_at == -1:
        errors.append(ProfileError(line_number, "缺冒号，要写成 属性：值"))
        return
    attribute = content[:separator_at].strip()
    values = [piece.strip() for piece in content[separator_at + 1 :].split(_VALUE_SEPARATOR)]
    if not attribute:
        errors.append(ProfileError(line_number, "冒号前面的属性是空的"))
    elif not all(values):
        errors.append(ProfileError(line_number, "冒号后面的值是空的（或顿号前后少了内容）"))
    else:
        entries.extend(ProfileEntry(subject, attribute, value) for value in values)


@dataclass(frozen=True)
class ProfilePlan:
    to_add: tuple[ProfileEntry, ...]
    to_remove: tuple[Fact, ...]
    unchanged: int


def plan_profile_import(
    entries: Sequence[ProfileEntry], existing_facts: Sequence[Fact]
) -> ProfilePlan:
    in_file = {_key(entry.subject, entry.attribute, entry.value): entry for entry in entries}
    in_db = {
        _key(fact.subject, fact.attribute, fact.value): fact
        for fact in existing_facts
        if fact.source is FactSource.PROFILE
    }
    to_add = tuple(entry for key, entry in in_file.items() if key not in in_db)
    to_remove = tuple(fact for key, fact in in_db.items() if key not in in_file)
    return ProfilePlan(to_add=to_add, to_remove=to_remove, unchanged=len(in_file) - len(to_add))


def _key(subject: str, attribute: str, value: str) -> tuple[str, str, str]:
    return (subject.strip().casefold(), attribute.strip().casefold(), value.strip().casefold())
