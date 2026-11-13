from typing import Protocol

from argus.situation.coordinates import Gcj02Point

SENT_DECIMALS = 3


def coarsened(point: Gcj02Point) -> Gcj02Point:
    return Gcj02Point(lat=round(point.lat, SENT_DECIMALS), lng=round(point.lng, SENT_DECIMALS))


class ReverseGeocodeError(Exception):
    pass


class ReverseGeocoder(Protocol):
    async def district_and_road(self, point: Gcj02Point) -> str: ...
