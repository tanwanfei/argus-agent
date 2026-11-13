from datetime import datetime, timedelta

from argus.kernel.clock import Clock
from argus.kernel.events import TurnCompleted
from argus.kernel.tools import Speaker
from argus.situation.geofence import Side
from argus.situation.geofence_watch import GeofenceWatch
from argus.situation.places import HOME_PLACE_NAME

HEARD_YOU_COUNTS_FOR = timedelta(minutes=10)


class HomePresence:
    def __init__(
        self, geofences: GeofenceWatch, clock: Clock, *, home_mic_endpoint_ids: frozenset[str]
    ) -> None:
        self._geofences = geofences
        self._clock = clock
        self._home_mic_endpoint_ids = home_mic_endpoint_ids
        self._heard_you_at: datetime | None = None

    async def on_turn_completed(self, event: TurnCompleted) -> None:
        if event.endpoint_id in self._home_mic_endpoint_ids and event.speaker is Speaker.OWNER:
            self._heard_you_at = event.ts

    def at_home(self) -> bool:
        heard_you_at = self._heard_you_at
        if heard_you_at is not None and self._clock.now() - heard_you_at <= HEARD_YOU_COUNTS_FOR:
            return True
        side = self._geofences.side_of(HOME_PLACE_NAME)
        return side is not Side.OUTSIDE
