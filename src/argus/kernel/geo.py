from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class GeoFix:
    lat: float
    lng: float
    accuracy_m: float
    at: datetime
    moving: bool
