_DIGITS = {
    "零": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
_UNITS = {"十": 10, "百": 100}


def parse_chinese_number(text: str) -> int | None:
    if not text or not all(char in _DIGITS or char in _UNITS for char in text):
        return None
    total = 0
    digit = 0
    for char in text:
        if char in _DIGITS:
            digit = _DIGITS[char]
        else:
            total += (digit or 1) * _UNITS[char]
            digit = 0
    return total + digit
