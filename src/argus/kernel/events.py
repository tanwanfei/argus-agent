from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Protocol

from argus.kernel.tools import Speaker
from argus.kernel.tracing import current_trace_id


@dataclass(frozen=True, kw_only=True)
class Event:
    ts: datetime
    trace_id: str | None = field(default_factory=current_trace_id)


class RouteKind(StrEnum):
    RULE = "rule"
    CONTROL = "control"
    CLOUD = "cloud"
    DEFER = "defer"


@dataclass(frozen=True)
class TurnCompleted(Event):
    endpoint_id: str
    speaker: Speaker
    asr_final_ms: int | None
    route_done_ms: int | None
    llm_first_token_ms: int | None
    tts_first_chunk_ms: int | None
    play_start_ms: int | None
    route: RouteKind | None


@dataclass(frozen=True)
class TurnRecorded(Event):
    speaker: Speaker
    utterance: str
    reply: str
    spoken: bool
    route: RouteKind | None
    tools_called: tuple[str, ...] = ()


@dataclass(frozen=True)
class MemoryForgotten(Event):
    topic: str


@dataclass(frozen=True)
class ProactiveDelivered(Event):
    text: str


@dataclass(frozen=True)
class DiskSpaceLow(Event):
    disk_name: str
    path: str
    free_gb: float
    threshold_gb: float


@dataclass(frozen=True)
class HomeAssistantUnreachable(Event):
    since: datetime
    reason: str
    user_action: str | None


@dataclass(frozen=True)
class BackupFailed(Event):
    reason: str


@dataclass(frozen=True)
class AppSignatureExpiring(Event):
    expires_at: datetime
    reported_at: datetime


@dataclass(frozen=True)
class CalendarUpcoming(Event):
    uid: str
    title: str
    start: datetime
    end: datetime
    location: str | None

    @property
    def dedupe_key(self) -> str:
        return f"{self.uid}@{self.start.astimezone(UTC).isoformat()}"


@dataclass(frozen=True)
class WeatherAlert(Event):
    alert_id: str
    headline: str
    description: str
    severity: str
    expires_at: datetime


@dataclass(frozen=True)
class TodayForecast(Event):
    day: date
    daytime_condition: str
    daytime_precipitation_probability: float
    nighttime_condition: str
    nighttime_precipitation_probability: float
    temperature_max_c: float
    yesterday_max_c: float | None


@dataclass(frozen=True)
class BirthdayToday(Event):
    who: str
    day: date
    lunar: str | None


@dataclass(frozen=True)
class BudgetThresholdReached(Event):
    ratio: float
    spent_yuan: float
    budget_yuan: float


@dataclass(frozen=True)
class CodeTaskFinished(Event):
    project: str
    session_id: str
    dispatched: bool = False
    finished: bool | None = None
    changed_files: int | None = None
    tests: str | None = None
    details: str | None = None
    where: str | None = None


@dataclass(frozen=True)
class CodeTaskNeedsAttention(Event):
    project: str
    session_id: str
    dispatched: bool = False
    question: str | None = None


@dataclass(frozen=True)
class CloudAvailabilityChanged(Event):
    available: bool
    reason: str


@dataclass(frozen=True)
class TimerDue(Event):
    job_id: str
    duration: str


@dataclass(frozen=True)
class TimerMissed(Event):
    job_id: str
    duration: str
    due_at: datetime


@dataclass(frozen=True)
class ReminderDue(Event):
    job_id: str
    what: str


@dataclass(frozen=True)
class ReminderMissed(Event):
    job_id: str
    what: str
    due_at: datetime


@dataclass(frozen=True)
class DeferredAnswer:
    utterance: str
    reply: str


@dataclass(frozen=True)
class DeferredTurnAnswered(Event):
    job_ids: tuple[str, ...]
    answers: tuple[DeferredAnswer, ...]


@dataclass(frozen=True)
class DeferredTurnGivenUp(Event):
    job_ids: tuple[str, ...]
    utterances: tuple[str, ...]


@dataclass(frozen=True)
class GeofenceEntered(Event):
    place: str


@dataclass(frozen=True)
class GeofenceExited(Event):
    place: str


@dataclass(frozen=True)
class PlaceReminderDue(Event):
    reminder_id: str
    place: str
    arrived: bool
    what: str


class EventBus(Protocol):
    def publish(self, event: Event) -> None: ...

    def subscribe[EventT: Event](
        self, event_type: type[EventT], handler: Callable[[EventT], Awaitable[None]]
    ) -> None: ...
