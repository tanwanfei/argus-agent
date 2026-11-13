from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from argus.kernel.geo import GeoFix
from argus.situation.coordinates import Wgs84Point, distance_m

MIN_RADIUS_M = 50.0
MAX_RADIUS_M = 2000.0
DEFAULT_RADIUS_M = 200.0
HOME_PLACE_NAME = "家"


@dataclass(frozen=True)
class Place:
    name: str
    center: Wgs84Point
    radius_m: float
    updated_at: datetime

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("名字不能为空")
        if not -90 <= self.center.lat <= 90:
            raise ValueError("纬度要在 -90～90 之间")
        if not -180 <= self.center.lng <= 180:
            raise ValueError("经度要在 -180～180 之间")
        if not MIN_RADIUS_M <= self.radius_m <= MAX_RADIUS_M:
            raise ValueError(f"半径要在 {MIN_RADIUS_M:g}～{MAX_RADIUS_M:g} 米之间")


class PlaceStore(Protocol):
    async def all(self) -> Sequence[Place]: ...


class PlaceBook(PlaceStore, Protocol):
    async def save(self, place: Place) -> None: ...

    async def remove(self, name: str) -> bool: ...


def describe_location(fix: GeoFix, places: Sequence[Place]) -> str | None:
    here = Wgs84Point(lat=fix.lat, lng=fix.lng)
    nearby: list[tuple[float, Place]] = []
    for place in places:
        if fix.accuracy_m > place.radius_m:
            continue
        distance = distance_m(here, place.center)
        if distance <= place.radius_m:
            nearby.append((distance, place))
    if not nearby:
        return None

    _, nearest = min(nearby, key=lambda candidate: candidate[0])
    return f"在{nearest.name}附近"
