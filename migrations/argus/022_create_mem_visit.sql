CREATE TABLE mem_visit (
    id INTEGER PRIMARY KEY,
    place TEXT NOT NULL,
    arrived INTEGER NOT NULL CHECK (arrived IN (0, 1)),
    at TEXT NOT NULL
) STRICT;
