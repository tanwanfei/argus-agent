from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta

from argus.delivery.outbound import Outbound
from argus.kernel.dialog import DialogState

INTERRUPTING_PREFIX = "先插一句："

_FREE_STATES = frozenset({DialogState.IDLE, DialogState.OPEN})


@dataclass(frozen=True)
class Announcement:
    message: Outbound
    text: str


class Announcer:
    def __init__(self, *, quiet_for: timedelta = timedelta(seconds=2)) -> None:
        self._quiet_for = quiet_for
        self._urgent: deque[Outbound] = deque()
        self._normal: deque[Outbound] = deque()
        self._expired: list[Outbound] = []
        self._state: DialogState | None = None
        self._free_since: datetime | None = None

    def enqueue(self, message: Outbound) -> None:
        (self._urgent if message.urgent else self._normal).append(message)

    def on_state(self, state: DialogState, now: datetime) -> None:
        if state in _FREE_STATES and not self._is_free():
            self._free_since = now
        self._state = state

    def on_announced(self, now: datetime) -> None:
        if self._is_free():
            self._free_since = now

    def next_when_free(self, now: datetime) -> Announcement | None:
        if not self._is_free():
            return None
        urgent = self._pop_unexpired(self._urgent, now)
        if urgent is not None:
            return Announcement(urgent, urgent.text)
        if not self._quiet_long_enough(now):
            return None
        normal = self._pop_unexpired(self._normal, now)
        if normal is not None:
            return Announcement(normal, normal.text)
        return None

    def next_between_sentences(self, now: datetime) -> Announcement | None:
        urgent = self._pop_unexpired(self._urgent, now)
        if urgent is None:
            return None
        return Announcement(urgent, INTERRUPTING_PREFIX + urgent.text)

    def wake_at(self, now: datetime) -> datetime | None:
        if not self._is_free() or not self._normal:
            return None
        if self._free_since is None:
            return now
        return max(now, self._free_since + self._quiet_for)

    def take_expired(self) -> list[Outbound]:
        expired, self._expired = self._expired, []
        return expired

    def _is_free(self) -> bool:
        return self._state is None or self._state in _FREE_STATES

    def _quiet_long_enough(self, now: datetime) -> bool:
        return self._free_since is None or now - self._free_since >= self._quiet_for

    def _pop_unexpired(self, queue: deque[Outbound], now: datetime) -> Outbound | None:
        while queue:
            message = queue.popleft()
            if now <= message.expires_at:
                return message
            self._expired.append(message)
        return None
