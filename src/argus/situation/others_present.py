from datetime import datetime, time, timedelta

from argus.kernel.clock import Clock
from argus.kernel.events import TurnCompleted
from argus.kernel.tools import Speaker

OTHER_VOICE_COUNTS_FOR = timedelta(minutes=30)
GUESTS_CLEARED_AT = time(6, 0)


class OthersPresence:
    def __init__(self, clock: Clock, *, home_mic_endpoint_ids: frozenset[str]) -> None:
        self._clock = clock
        self._home_mic_endpoint_ids = home_mic_endpoint_ids
        self._heard_other_at: datetime | None = None
        self._guests_until: datetime | None = None

    async def on_turn_completed(self, event: TurnCompleted) -> None:
        if event.endpoint_id in self._home_mic_endpoint_ids and event.speaker is Speaker.OTHER:
            self._heard_other_at = event.ts

    def guests_arrived(self) -> None:
        self._guests_until = _next_clearing_time(self._clock.now())

    def guests_left(self) -> None:
        self._guests_until = None
        self._heard_other_at = None

    def present(self) -> bool:
        now = self._clock.now()
        if self._guests_until is not None and now < self._guests_until:
            return True
        heard_other_at = self._heard_other_at
        return heard_other_at is not None and now - heard_other_at <= OTHER_VOICE_COUNTS_FOR


def _next_clearing_time(now: datetime) -> datetime:
    today_at_six = datetime.combine(now.date(), GUESTS_CLEARED_AT, tzinfo=now.tzinfo)
    return today_at_six if today_at_six > now else today_at_six + timedelta(days=1)
