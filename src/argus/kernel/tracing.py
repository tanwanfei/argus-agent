from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from enum import StrEnum

from argus.kernel.clock import Clock

_trace_id: ContextVar[str | None] = ContextVar("trace_id", default=None)


def current_trace_id() -> str | None:
    return _trace_id.get()


@contextmanager
def trace(trace_id: str) -> Generator[None]:
    token = _trace_id.set(trace_id)
    try:
        yield
    finally:
        _trace_id.reset(token)


class LatencyMark(StrEnum):
    ASR_FINAL = "asr.final"
    ROUTE_DONE = "route.done"
    LLM_FIRST_TOKEN = "llm.first_token"
    TTS_FIRST_CHUNK = "tts.first_chunk"
    PLAY_START = "play.start"


class LatencyMarks:
    def __init__(self, clock: Clock, vad_end: float) -> None:
        self._clock = clock
        self._vad_end = vad_end
        self._marked_at: dict[LatencyMark, float] = {}

    def mark(self, mark: LatencyMark, *, delay_seconds: float = 0.0) -> None:
        self.mark_at(mark, self._clock.monotonic() + delay_seconds)

    def mark_at(self, mark: LatencyMark, monotonic: float) -> None:
        self._marked_at.setdefault(mark, monotonic)

    def ms_since_vad_end(self, mark: LatencyMark) -> int | None:
        marked_at = self._marked_at.get(mark)
        if marked_at is None:
            return None
        return round((marked_at - self._vad_end) * 1000)


_latency_marks: ContextVar[LatencyMarks | None] = ContextVar("latency_marks", default=None)


@contextmanager
def recording_latency(marks: LatencyMarks) -> Generator[None]:
    token = _latency_marks.set(marks)
    try:
        yield
    finally:
        _latency_marks.reset(token)


def mark_latency(mark: LatencyMark, *, delay_seconds: float = 0.0) -> None:
    marks = _latency_marks.get()
    if marks is not None:
        marks.mark(mark, delay_seconds=delay_seconds)
