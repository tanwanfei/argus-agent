import sqlite3
from datetime import datetime, time

from argus.infra.sqlite.database import SqliteDatabase
from argus.proactive.place_reminder import Crossing, PlaceReminder

_COLUMNS = (
    "id, place, crossing, what, every_time, created_at, workdays_only, window_start, window_end"
)
_INSERT = f"INSERT INTO pro_place_reminder ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
_SELECT_ALL = f"SELECT {_COLUMNS} FROM pro_place_reminder ORDER BY rowid"
_DELETE = "DELETE FROM pro_place_reminder WHERE id = ?"


class SqlitePlaceReminders:
    def __init__(self, database: SqliteDatabase) -> None:
        self._database = database

    async def add(self, reminder: PlaceReminder) -> None:
        row = (
            reminder.id,
            reminder.place,
            reminder.crossing.value,
            reminder.what,
            int(reminder.every_time),
            reminder.created_at.isoformat(),
            int(reminder.workdays_only),
            _clock_time_text(reminder.window_start),
            _clock_time_text(reminder.window_end),
        )

        def insert(connection: sqlite3.Connection) -> None:
            connection.execute(_INSERT, row)

        await self._database.write(insert)

    async def all(self) -> list[PlaceReminder]:
        def select_all(connection: sqlite3.Connection) -> list[PlaceReminder]:
            return [_reminder_of(row) for row in connection.execute(_SELECT_ALL)]

        return await self._database.read(select_all)

    async def remove(self, reminder_id: str) -> bool:
        def delete(connection: sqlite3.Connection) -> bool:
            return connection.execute(_DELETE, (reminder_id,)).rowcount > 0

        return await self._database.write(delete)


def _reminder_of(
    row: tuple[str, str, str, str, int, str, int, str | None, str | None],
) -> PlaceReminder:
    reminder_id, place, crossing, what, every_time, created_at, workdays_only, start, end = row
    return PlaceReminder(
        id=reminder_id,
        place=place,
        crossing=Crossing(crossing),
        what=what,
        every_time=bool(every_time),
        created_at=datetime.fromisoformat(created_at),
        workdays_only=bool(workdays_only),
        window_start=None if start is None else time.fromisoformat(start),
        window_end=None if end is None else time.fromisoformat(end),
    )


def _clock_time_text(clock_time: time | None) -> str | None:
    return None if clock_time is None else clock_time.isoformat(timespec="minutes")
