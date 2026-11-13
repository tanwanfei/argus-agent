from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

from argus.kernel.events import GeofenceEntered, GeofenceExited
from argus.kernel.geo import GeoFix
from argus.situation.coordinates import Wgs84Point, distance_m
from argus.situation.places import Place

DWELL = timedelta(minutes=2)
EXIT_RADIUS_FACTOR = 1.5


class Side(Enum):
    INSIDE = "inside"
    OUTSIDE = "outside"


@dataclass(frozen=True)
class FenceState:
    side: Side | None
    crossed_since: datetime | None = None


UNKNOWN = FenceState(side=None)


def after_fix(state: FenceState, place: Place, fix: GeoFix) -> FenceState:
    seen_side = _side_of(place, fix)
    if seen_side is None:
        return state
    if state.side is None:
        return FenceState(side=seen_side)
    if seen_side is state.side:
        return FenceState(side=state.side)
    if state.crossed_since is not None:
        return state
    return FenceState(side=state.side, crossed_since=fix.at)


def advance(
    state: FenceState, place: Place, *, now: datetime
) -> tuple[FenceState, GeofenceEntered | GeofenceExited | None]:
    if state.crossed_since is None or now - state.crossed_since < DWELL:
        return state, None
    if state.side is Side.OUTSIDE:
        return FenceState(side=Side.INSIDE), GeofenceEntered(ts=now, place=place.name)
    return FenceState(side=Side.OUTSIDE), GeofenceExited(ts=now, place=place.name)


def _side_of(place: Place, fix: GeoFix) -> Side | None:
    distance = distance_m(Wgs84Point(lat=fix.lat, lng=fix.lng), place.center)
    if distance <= place.radius_m and fix.accuracy_m <= place.radius_m:
        return Side.INSIDE
    if distance - fix.accuracy_m > place.radius_m * EXIT_RADIUS_FACTOR:
        return Side.OUTSIDE
    return None
