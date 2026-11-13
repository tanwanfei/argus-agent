import sqlite3
from datetime import datetime

from argus.infra.sqlite.database import SqliteDatabase
from argus.situation.coordinates import Wgs84Point
from argus.situation.places import Place

_UPSERT_PLACE = """
INSERT INTO sit_place (name, lat, lng, radius_m, updated_at)
VALUES (:name, :lat, :lng, :radius_m, :updated_at)
ON CONFLICT (name) DO UPDATE SET
    lat = excluded.lat,
    lng = excluded.lng,
    radius_m = excluded.radius_m,
    updated_at = excluded.updated_at
"""

_SELECT_ALL = "SELECT name, lat, lng, radius_m, updated_at FROM sit_place ORDER BY name"

_DELETE_BY_NAME = "DELETE FROM sit_place WHERE name = ?"


class SqlitePlaceStore:
    def __init__(self, database: SqliteDatabase) -> None:
        self._database = database

    async def save(self, place: Place) -> None:
        row = {
            "name": place.name,
            "lat": place.center.lat,
            "lng": place.center.lng,
            "radius_m": place.radius_m,
            "updated_at": place.updated_at.isoformat(),
        }

        def upsert(connection: sqlite3.Connection) -> None:
            connection.execute(_UPSERT_PLACE, row)

        await self._database.write(upsert)

    async def all(self) -> list[Place]:
        def select_all(
            connection: sqlite3.Connection,
        ) -> list[tuple[str, float, float, float, str]]:
            return connection.execute(_SELECT_ALL).fetchall()

        rows = await self._database.read(select_all)
        return [
            Place(
                name=name,
                center=Wgs84Point(lat=lat, lng=lng),
                radius_m=radius_m,
                updated_at=datetime.fromisoformat(updated_at),
            )
            for name, lat, lng, radius_m, updated_at in rows
        ]

    async def remove(self, name: str) -> bool:
        def delete(connection: sqlite3.Connection) -> bool:
            return connection.execute(_DELETE_BY_NAME, (name,)).rowcount > 0

        return await self._database.write(delete)
