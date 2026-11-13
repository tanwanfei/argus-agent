import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from argus.kernel.clock import Clock
from argus.kernel.geo import GeoFix
from argus.situation.coordinates import Gcj02Point, wgs84_to_gcj02
from argus.situation.geocoding import ReverseGeocodeError, ReverseGeocoder, coarsened
from argus.situation.geofence_watch import GeofenceWatch
from argus.situation.places import Place, PlaceStore, describe_location

logger = logging.getLogger(__name__)

FIX_STALE_AFTER = timedelta(minutes=10)

GEOCODE_WAIT_S = 1.0

LOCATION_WITHOUT_NAME = "手机报了位置，地名没查到"


@dataclass(frozen=True)
class _Lookup:
    sent_point: Gcj02Point
    district_and_road: str


class Situation:
    def __init__(
        self,
        *,
        geocoder: ReverseGeocoder | None,
        places: PlaceStore,
        clock: Clock,
        geofences: GeofenceWatch,
    ) -> None:
        self._latest_fix: GeoFix | None = None
        self._geocoder = geocoder
        self._places = places
        self._clock = clock
        self._geofences = geofences
        self._last_lookup: _Lookup | None = None

    def update_location(self, fix: GeoFix) -> None:
        self._latest_fix = fix
        self._geofences.update_location(fix)

    def precise(self, now: datetime) -> GeoFix | None:
        if self._latest_fix is None:
            return None
        if now - self._latest_fix.at > FIX_STALE_AFTER:
            return None
        return self._latest_fix

    async def for_cloud(self, now: datetime) -> str | None:
        fix = self.precise(now)
        if fix is None:
            return None
        near_place = describe_location(fix, await self._recorded_places())
        if near_place is not None:
            return near_place
        district_and_road = await self._district_and_road(fix)
        if district_and_road is None:
            return LOCATION_WITHOUT_NAME
        return f"在{district_and_road}附近"

    async def _recorded_places(self) -> Sequence[Place]:
        try:
            return await self._places.all()
        except Exception:
            logger.exception("地点表读不出来，这回合不认录过的地点")
            return []

    async def _district_and_road(self, fix: GeoFix) -> str | None:
        if self._geocoder is None:
            return None
        sent_point = coarsened(wgs84_to_gcj02(lat=fix.lat, lng=fix.lng))
        if self._last_lookup is not None and self._last_lookup.sent_point == sent_point:
            return self._last_lookup.district_and_road

        try:
            async with self._clock.timeout(GEOCODE_WAIT_S):
                district_and_road = await self._geocoder.district_and_road(sent_point)
        except ReverseGeocodeError as error:
            logger.warning("反查地名没查到：%s", error)
            return None
        except TimeoutError:
            logger.warning("反查地名 %g 秒还没查完，这回合不说在哪条路上", GEOCODE_WAIT_S)
            return None
        self._last_lookup = _Lookup(sent_point, district_and_road)
        return district_and_road
