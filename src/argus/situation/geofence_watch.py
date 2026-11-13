import logging

from argus.kernel.clock import Clock
from argus.kernel.events import EventBus, GeofenceEntered
from argus.kernel.geo import GeoFix
from argus.situation.geofence import DWELL, UNKNOWN, FenceState, Side, advance, after_fix
from argus.situation.places import PlaceStore

logger = logging.getLogger(__name__)

_CHECK_EVERY_S = 20

_TOO_OLD_ON_ARRIVAL = DWELL


class GeofenceWatch:
    def __init__(self, places: PlaceStore, clock: Clock, bus: EventBus) -> None:
        self._places = places
        self._clock = clock
        self._bus = bus
        self._unchecked_fixes: list[GeoFix] = []
        self._state_by_place: dict[str, FenceState] = {}

    def update_location(self, fix: GeoFix) -> None:
        if self._clock.now() - fix.at > _TOO_OLD_ON_ARRIVAL:
            return
        self._unchecked_fixes.append(fix)

    def side_of(self, place_name: str) -> Side | None:
        return self._state_by_place.get(place_name, UNKNOWN).side

    async def run(self) -> None:
        while True:
            await self.check_once()
            await self._clock.sleep(_CHECK_EVERY_S)

    async def check_once(self) -> None:
        waiting = any(state.crossed_since is not None for state in self._state_by_place.values())
        if not self._unchecked_fixes and not waiting:
            return
        try:
            places = await self._places.all()
        except Exception:
            logger.exception("地点表读不出来，围栏这一轮不算")
            return
        fixes, self._unchecked_fixes = self._unchecked_fixes, []
        now = self._clock.now()
        state_by_place: dict[str, FenceState] = {}
        for place in places:
            state = self._state_by_place.get(place.name, UNKNOWN)
            for fix in fixes:
                state = after_fix(state, place, fix)
            state_by_place[place.name], event = advance(state, place, now=now)
            if event is None:
                continue
            self._bus.publish(event)
            logger.info(
                "围栏：%s一个录过的地点", "进了" if isinstance(event, GeofenceEntered) else "离开了"
            )
        self._state_by_place = state_by_place
