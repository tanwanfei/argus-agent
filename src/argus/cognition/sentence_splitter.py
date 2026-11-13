_SENTENCE_END_MARKS = "。！？!?；;….\n"
_CLOSING_MARKS = "”’」』）)》】]\"'"
_COMMAS = "，,"
_MIN_CHARS_BEFORE_FIRST_COMMA = 2


class SentenceSplitter:
    def __init__(self) -> None:
        self._pending = ""
        self._first_sentence_done = False

    def feed(self, text: str) -> list[str]:
        self._pending += text
        sentences: list[str] = []
        while True:
            cut = self._find_cut(self._pending)
            if cut is None:
                return sentences
            sentences.append(self._pending[:cut].strip())
            self._pending = self._pending[cut:]
            self._first_sentence_done = True

    def flush(self) -> list[str]:
        last_sentence = self._pending.strip()
        self._pending = ""
        return [last_sentence] if last_sentence else []

    def _find_cut(self, text: str) -> int | None:
        index = 0
        while index < len(text):
            char = text[index]
            if char in _SENTENCE_END_MARKS:
                marks_end = _skip_marks(text, index, _SENTENCE_END_MARKS)
                needs_space_after = text[index:marks_end] == "."
            elif (
                char in _COMMAS
                and not self._first_sentence_done
                and _count_spoken_chars(text[:index]) >= _MIN_CHARS_BEFORE_FIRST_COMMA
            ):
                marks_end = index + 1
                needs_space_after = char == ","
            else:
                index += 1
                continue

            cut = _skip_marks(text, marks_end, _CLOSING_MARKS)
            if cut == len(text):
                return None
            is_real_break = not needs_space_after or text[cut].isspace()
            if is_real_break and _count_spoken_chars(text[:index]) > 0:
                return cut
            index = cut
        return None


def _skip_marks(text: str, start: int, marks: str) -> int:
    position = start
    while position < len(text) and text[position] in marks:
        position += 1
    return position


def _count_spoken_chars(text: str) -> int:
    return sum(1 for char in text if char.isalnum())
