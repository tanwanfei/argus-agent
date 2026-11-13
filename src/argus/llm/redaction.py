import re
import unicodedata
from collections import Counter
from collections.abc import Callable, Iterable
from enum import StrEnum


class Kind(StrEnum):
    PHONE = "PHONE"
    ID_CARD = "IDCARD"
    BANK_CARD = "CARD"
    EMAIL = "EMAIL"
    ADDRESS = "ADDR"
    CODE = "CODE"
    PASSWORD = "PWD"
    WORD = "WORD"


PLACEHOLDER_PREFIX = "‹"
_PLACEHOLDER = re.compile(r"(?P<placeholder>‹[A-Z]+_\d+›)(?P<space> (?=(?P<next>\S)))?")


class Placeholders:
    def __init__(self) -> None:
        self._placeholder_by_value: dict[tuple[Kind, str], str] = {}
        self._original_by_placeholder: dict[str, str] = {}
        self._count_by_kind: Counter[Kind] = Counter()

    def placeholder_for(self, kind: Kind, original: str) -> str:
        key = (kind, _normalize(kind, original))
        placeholder = self._placeholder_by_value.get(key)
        if placeholder is None:
            self._count_by_kind[kind] += 1
            placeholder = f"‹{kind}_{self._count_by_kind[kind]}›"
            self._placeholder_by_value[key] = placeholder
            self._original_by_placeholder[placeholder] = original
        return placeholder

    def restore(self, text: str) -> str:
        def original_of(match: re.Match[str]) -> str:
            original = self._original_by_placeholder.get(match.group("placeholder"))
            if original is None:
                return match.group(0)
            space = match.group("space") or ""
            if space and _is_wide(original[-1]) and _is_wide(match.group("next")):
                space = ""
            return original + space

        return _PLACEHOLDER.sub(original_of, text)

    def __bool__(self) -> bool:
        return bool(self._original_by_placeholder)


def _is_wide(char: str) -> bool:
    return unicodedata.east_asian_width(char) in ("W", "F")


def _normalize(kind: Kind, original: str) -> str:
    if kind is Kind.PHONE:
        digits = re.sub(r"\D", "", original)
        for country_code in ("0086", "86"):
            if len(digits) == len(country_code) + 11 and digits.startswith(country_code):
                return digits[len(country_code) :]
        return digits
    if kind is Kind.BANK_CARD:
        return re.sub(r"\D", "", original)
    if kind in (Kind.ID_CARD, Kind.EMAIL):
        return original.lower()
    return original


type _Rule = Callable[[str, Placeholders], str]


def _replace_group(
    pattern: re.Pattern[str],
    kind: Kind,
    *,
    group: int = 0,
    accept: Callable[[str], bool] = lambda _: True,
) -> _Rule:
    def apply(text: str, placeholders: Placeholders) -> str:
        def replace(match: re.Match[str]) -> str:
            value = match.group(group)
            if not accept(value):
                return match.group(0)
            start, end = match.start(group) - match.start(), match.end(group) - match.start()
            whole = match.group(0)
            return whole[:start] + placeholders.placeholder_for(kind, value) + whole[end:]

        return pattern.sub(replace, text)

    return apply


def _id_card_checksum_ok(value: str) -> bool:
    weights = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
    check_chars = "10X98765432"
    total = sum(int(digit) * weight for digit, weight in zip(value[:17], weights, strict=True))
    return check_chars[total % 11] == value[17].upper()


def _luhn_ok(value: str) -> bool:
    digits = [int(char) for char in re.sub(r"\D", "", value)]
    if not 16 <= len(digits) <= 19:
        return False
    total = 0
    for position, digit in enumerate(reversed(digits)):
        if position % 2 == 1:
            digit = digit * 2 - 9 if digit * 2 > 9 else digit * 2
        total += digit
    return total % 10 == 0


_FIXED_RULES: tuple[_Rule, ...] = (
    _replace_group(re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), Kind.EMAIL),
    _replace_group(
        re.compile(r"(?<![0-9A-Za-z])\d{17}[\dXx](?![0-9A-Za-z])"),
        Kind.ID_CARD,
        accept=_id_card_checksum_ok,
    ),
    _replace_group(
        re.compile(r"(?<!\d)\d{4}(?:[ -]?\d{4}){3}(?:[ -]?\d{1,3})?(?!\d)"),
        Kind.BANK_CARD,
        accept=_luhn_ok,
    ),
    _replace_group(
        re.compile(r"(?<!\d)(?:(?:\+|00)?86[- ]?)?1[3-9]\d(?:[- ]?\d{4}){2}(?!\d)"), Kind.PHONE
    ),
    _replace_group(re.compile(r"(?<!\d)0\d{2,3}-\d{7,8}(?!\d)"), Kind.PHONE),
    _replace_group(
        re.compile(
            r"(?:验证码|校验码|动态码|动态密码|短信码)\s*(?:是|为)?\s*[:：]?\s*([0-9A-Za-z]{4,8})(?![0-9A-Za-z])"
        ),
        Kind.CODE,
        group=1,
    ),
    _replace_group(
        re.compile(r"(?:密码|口令|[Pp][Ii][Nn]码?)\s*(?:是|为)?\s*[:：]?\s*([!-~]{4,32})"),
        Kind.PASSWORD,
        group=1,
    ),
    _replace_group(
        re.compile(
            r"(?:路|街|大道|巷|胡同|村|小区|花园|公寓|大厦|苑)"
            r"(\d+(?:-\d+)?号(?!线)(?:院|楼)?(?:\d+(?:号楼|栋|幢|座))?(?:\d+单元)?(?:\d+室?)?)"
        ),
        Kind.ADDRESS,
        group=1,
    ),
    _replace_group(
        re.compile(r"(?<!\d)(?:\d+(?:号楼|栋|幢|座)\d+单元(?:\d+室?)?|\d+单元\d+室?|\d{3,4}室)"),
        Kind.ADDRESS,
    ),
)


class Redactor:
    def __init__(self, sensitive_words: Iterable[str] = ()) -> None:
        words = sorted(
            {word.strip() for word in sensitive_words if word.strip()}, key=len, reverse=True
        )
        self._rules = list(_FIXED_RULES)
        if words:
            pattern = re.compile("|".join(re.escape(word) for word in words))
            self._rules.append(_replace_group(pattern, Kind.WORD))

    def redact(self, text: str, placeholders: Placeholders) -> str:
        for rule in self._rules:
            text = rule(text, placeholders)
        return text
