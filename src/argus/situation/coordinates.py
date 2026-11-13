import math
from dataclasses import dataclass

_SEMI_MAJOR_AXIS_M = 6378245.0
_ECCENTRICITY_SQUARED = 0.00669342162296594323

_CHINA_MIN_LNG = 73.66
_CHINA_MAX_LNG = 135.05
_CHINA_MIN_LAT = 3.86
_CHINA_MAX_LAT = 53.55

_EARTH_MEAN_RADIUS_M = 6371008.8

_INVERSE_TOLERANCE_DEG = 1e-7
_INVERSE_MAX_STEPS = 10


@dataclass(frozen=True)
class Wgs84Point:
    lat: float
    lng: float


@dataclass(frozen=True)
class Gcj02Point:
    lat: float
    lng: float


def wgs84_to_gcj02(*, lat: float, lng: float) -> Gcj02Point:
    if not _inside_china(lat=lat, lng=lng):
        return Gcj02Point(lat=lat, lng=lng)

    north_offset_m = _north_offset_m(x=lng - 105.0, y=lat - 35.0)
    east_offset_m = _east_offset_m(x=lng - 105.0, y=lat - 35.0)

    lat_rad = math.radians(lat)
    sin_lat = math.sin(lat_rad)
    w_squared = 1 - _ECCENTRICITY_SQUARED * sin_lat * sin_lat
    meridian_radius_m = (
        _SEMI_MAJOR_AXIS_M * (1 - _ECCENTRICITY_SQUARED) / (w_squared * math.sqrt(w_squared))
    )
    prime_vertical_radius_m = _SEMI_MAJOR_AXIS_M / math.sqrt(w_squared)
    lat_offset_deg = math.degrees(north_offset_m / meridian_radius_m)
    lng_offset_deg = math.degrees(east_offset_m / (prime_vertical_radius_m * math.cos(lat_rad)))
    return Gcj02Point(lat=lat + lat_offset_deg, lng=lng + lng_offset_deg)


def gcj02_to_wgs84(point: Gcj02Point) -> Wgs84Point:
    if not _inside_china(lat=point.lat, lng=point.lng):
        return Wgs84Point(lat=point.lat, lng=point.lng)

    guess_lat, guess_lng = point.lat, point.lng
    for _ in range(_INVERSE_MAX_STEPS):
        forward = wgs84_to_gcj02(lat=guess_lat, lng=guess_lng)
        lat_error = forward.lat - point.lat
        lng_error = forward.lng - point.lng
        if abs(lat_error) < _INVERSE_TOLERANCE_DEG and abs(lng_error) < _INVERSE_TOLERANCE_DEG:
            break
        guess_lat -= lat_error
        guess_lng -= lng_error
    return Wgs84Point(lat=guess_lat, lng=guess_lng)


def looks_swapped(*, lat: float, lng: float) -> bool:
    return not _inside_china(lat=lat, lng=lng) and _inside_china(lat=lng, lng=lat)


def distance_m(a: Wgs84Point, b: Wgs84Point) -> float:
    lat_a = math.radians(a.lat)
    lat_b = math.radians(b.lat)
    half_lat_diff = math.radians(b.lat - a.lat) / 2
    half_lng_diff = math.radians(b.lng - a.lng) / 2
    half_angle_sin_squared = (
        math.sin(half_lat_diff) ** 2
        + math.cos(lat_a) * math.cos(lat_b) * math.sin(half_lng_diff) ** 2
    )
    central_angle = 2 * math.asin(min(1.0, math.sqrt(half_angle_sin_squared)))
    return _EARTH_MEAN_RADIUS_M * central_angle


def _inside_china(*, lat: float, lng: float) -> bool:
    return _CHINA_MIN_LNG < lng < _CHINA_MAX_LNG and _CHINA_MIN_LAT < lat < _CHINA_MAX_LAT


def _north_offset_m(*, x: float, y: float) -> float:
    offset = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
    offset += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    offset += (20.0 * math.sin(y * math.pi) + 40.0 * math.sin(y / 3.0 * math.pi)) * 2.0 / 3.0
    offset += (
        (160.0 * math.sin(y / 12.0 * math.pi) + 320.0 * math.sin(y * math.pi / 30.0)) * 2.0 / 3.0
    )
    return offset


def _east_offset_m(*, x: float, y: float) -> float:
    offset = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
    offset += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    offset += (20.0 * math.sin(x * math.pi) + 40.0 * math.sin(x / 3.0 * math.pi)) * 2.0 / 3.0
    offset += (
        (150.0 * math.sin(x / 12.0 * math.pi) + 300.0 * math.sin(x / 30.0 * math.pi)) * 2.0 / 3.0
    )
    return offset
