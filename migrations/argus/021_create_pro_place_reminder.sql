CREATE TABLE pro_place_reminder (
    id TEXT NOT NULL PRIMARY KEY,
    place TEXT NOT NULL,
    crossing TEXT NOT NULL CHECK (crossing IN ('arrive', 'leave')),
    what TEXT NOT NULL,
    every_time INTEGER NOT NULL CHECK (every_time IN (0, 1)),
    created_at TEXT NOT NULL,
    workdays_only INTEGER NOT NULL CHECK (workdays_only IN (0, 1)),
    window_start TEXT,
    window_end TEXT
) STRICT;
