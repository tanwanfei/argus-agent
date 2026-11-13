import re
from dataclasses import dataclass

CHUNK_MAX_CHARS = 500
OVERLAP_MAX_CHARS = 100

_HEADING = re.compile(r"^#{1,6}\s+(.*)$")
_QUESTION = re.compile(r"^\s*(?:[-*+]\s|\d+\s*[.、)）])")
_SENTENCE_END = re.compile(r"(?<=[。！？!?；;…])")


@dataclass(frozen=True)
class Chunk:
    label: str
    text: str


def chunk_document(text: str, default_heading: str) -> list[Chunk]:
    chunks: list[Chunk] = []
    heading = default_heading
    question: str | None = None
    answer_lines: list[str] = []

    def close_question() -> None:
        if question is not None:
            chunks.extend(
                _pieces(f"{heading} · {question}", f"【{heading}】{question}\n", answer_lines)
            )

    for raw_line in text.split("\n"):
        line = raw_line.strip()
        heading_match = _HEADING.match(line)
        if heading_match:
            close_question()
            heading, question, answer_lines = heading_match.group(1).strip(), None, []
        elif _QUESTION.match(line):
            close_question()
            question, answer_lines = line, []
        elif not line:
            continue
        elif question is not None:
            answer_lines.append(line)
        else:
            chunks.extend(_pieces(heading, f"【{heading}】", [line]))
    close_question()
    return chunks


def _pieces(label: str, header: str, body_lines: list[str]) -> list[Chunk]:
    body = "\n".join(body_lines)
    if len(header) + len(body) <= CHUNK_MAX_CHARS:
        return [Chunk(label, header + body)]
    budget = max(CHUNK_MAX_CHARS - len(header), 100)
    return [Chunk(label, header + piece) for piece in _split(body, budget)]


def _split(body: str, budget: int) -> list[str]:
    sentences = [
        sentence[start : start + budget]
        for sentence in _SENTENCE_END.split(body)
        if sentence
        for start in range(0, len(sentence), budget)
    ]
    pieces: list[str] = []
    current: list[str] = []
    for sentence in sentences:
        if current and len("".join(current)) + len(sentence) > budget:
            pieces.append("".join(current))
            last = current[-1]
            overlaps = len(last) <= OVERLAP_MAX_CHARS and len(last) + len(sentence) <= budget
            current = [last] if overlaps else []
        current.append(sentence)
    pieces.append("".join(current))
    return pieces
