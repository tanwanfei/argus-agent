import logging
from datetime import datetime, timedelta
from typing import Protocol

from argus.kernel.clock import Clock
from argus.kernel.events import EventBus, HomeAssistantUnreachable
from argus.kernel.tools import ToolFailure

logger = logging.getLogger(__name__)

_CHECK_INTERVAL_SECONDS = 60
_FAILURES_BEFORE_ALERT = 3
_REPEAT_AFTER = timedelta(hours=1)


class HomeAssistantProbe(Protocol):
    async def ping(self) -> ToolFailure | None: ...


class HomeAssistantWatch:
    def __init__(self, home_assistant: HomeAssistantProbe, clock: Clock, bus: EventBus) -> None:
        self._home_assistant = home_assistant
        self._clock = clock
        self._bus = bus
        self._last_failure: ToolFailure | None = None
        self._checked_once = False
        self._failures_in_a_row = 0
        self._failing_since: datetime | None = None
        self._last_alert_at: datetime | None = None

    async def run(self) -> None:
        while True:
            await self.check_once()
            await self._clock.sleep(_CHECK_INTERVAL_SECONDS)

    async def check_once(self) -> None:
        now = self._clock.now()
        failure = await self._home_assistant.ping()
        self._checked_once = True
        self._last_failure = failure
        if failure is None:
            self._recover()
            return
        self._failures_in_a_row += 1
        if self._failing_since is None:
            self._failing_since = now
            logger.warning("Home Assistant 探不通了：%s", failure.reason)
        if self._failures_in_a_row < _FAILURES_BEFORE_ALERT:
            return
        if self._last_alert_at is not None and now - self._last_alert_at < _REPEAT_AFTER:
            return
        self._last_alert_at = now
        self._bus.publish(
            HomeAssistantUnreachable(
                since=self._failing_since,
                reason=failure.reason,
                user_action=failure.user_action,
                ts=now,
            )
        )

    def report(self) -> dict[str, bool | str | None]:
        if not self._checked_once:
            return {"reachable": None}
        if self._last_failure is None:
            return {"reachable": True}
        return {"reachable": False, "reason": self._last_failure.reason}

    def _recover(self) -> None:
        if self._failing_since is not None:
            logger.info("Home Assistant 又能连上了")
        self._failures_in_a_row = 0
        self._failing_since = None
        self._last_alert_at = None
