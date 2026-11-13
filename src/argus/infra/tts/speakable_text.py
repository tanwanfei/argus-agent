import re

_DIGITS = "零一二三四五六七八九"

_MEASURE_WORDS_READ_AS_LIANG = "个|点|天|次|位|种|件|条|本|张|杯|年|周|岁|小时|分钟|斤|公里|块"
_MAX_DIGITS_READ_AS_AMOUNT = 8

_THOUSANDS_SEPARATOR = re.compile(r"(?<=\d),(?=\d{3}(?!\d))")
_CLOCK_TIME = re.compile(r"(?<![\d.])(\d{1,2})[:：](\d{2})(?!\d)")
_NUMBER = r"\d+(?:\.\d+)?"
_PERCENT = re.compile(rf"({_NUMBER})\s*[%％]")
_BELOW_ZERO_TEMPERATURE = re.compile(rf"[-−－]({_NUMBER})\s*(℃|°C|°|度)")
_DEGREE_SIGN = re.compile(r"℃|°C|°")
_YEAR = re.compile(r"(?<!\d)(\d{4})(?=\s*年)")
_TWO_BEFORE_MEASURE_WORD = re.compile(rf"(?<![\d.])2(?=\s*(?:{_MEASURE_WORDS_READ_AS_LIANG}))")
_ANY_NUMBER = re.compile(_NUMBER)

_SPEAKABLE_CHAR = re.compile(r"[㐀-䶿一-鿿A-Za-z]")


def spell_out_numbers(text: str) -> str:
    text = _THOUSANDS_SEPARATOR.sub("", text)
    text = _CLOCK_TIME.sub(_read_clock_time, text)
    text = _PERCENT.sub(lambda match: "百分之" + _read_number(match.group(1)), text)
    text = _BELOW_ZERO_TEMPERATURE.sub(lambda match: f"零下{_read_number(match.group(1))}度", text)
    text = _DEGREE_SIGN.sub("度", text)
    text = _YEAR.sub(lambda match: _read_digit_by_digit(match.group(1)), text)
    text = _TWO_BEFORE_MEASURE_WORD.sub("两", text)
    return _ANY_NUMBER.sub(lambda match: _read_number(match.group(0)), text)


def has_speakable_text(text: str) -> bool:
    return _SPEAKABLE_CHAR.search(text) is not None


def _read_clock_time(match: re.Match[str]) -> str:
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 24 or minute > 59:
        return match.group(0)
    hour_reading = "两" if hour == 2 else _read_integer(hour)
    if minute == 0:
        return f"{hour_reading}点"
    minute_reading = ("零" if minute < 10 else "") + _read_integer(minute)
    return f"{hour_reading}点{minute_reading}"


def _read_number(number: str) -> str:
    whole, _, fraction = number.partition(".")
    reading = _read_whole_number(whole)
    if fraction:
        reading += "点" + _read_digit_by_digit(fraction)
    return reading


def _read_whole_number(digits: str) -> str:
    if (len(digits) > 1 and digits.startswith("0")) or len(digits) > _MAX_DIGITS_READ_AS_AMOUNT:
        return _read_digit_by_digit(digits)
    return _read_integer(int(digits))


def _read_digit_by_digit(digits: str) -> str:
    return "".join(_DIGITS[int(digit)] for digit in digits)


def _read_integer(number: int) -> str:
    if number == 0:
        return "零"
    ten_thousands, below_ten_thousand = divmod(number, 10_000)
    if ten_thousands == 0:
        reading = _read_below_ten_thousand(below_ten_thousand)
    else:
        reading = _read_below_ten_thousand(ten_thousands) + "万"
        if below_ten_thousand:
            if below_ten_thousand < 1000:
                reading += "零"
            reading += _read_below_ten_thousand(below_ten_thousand)
    if reading.startswith("一十"):
        reading = reading[1:]
    if reading.startswith(("二千", "二万")):
        reading = "两" + reading[1:]
    return reading


def _read_below_ten_thousand(number: int) -> str:
    parts: list[str] = []
    zero_pending = False
    for place_value, unit in ((1000, "千"), (100, "百"), (10, "十"), (1, "")):
        digit = number // place_value % 10
        if digit == 0:
            zero_pending = bool(parts)
            continue
        if zero_pending:
            parts.append("零")
            zero_pending = False
        parts.append(_DIGITS[digit] + unit)
    return "".join(parts)
