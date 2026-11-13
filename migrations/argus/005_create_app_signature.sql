CREATE TABLE app_signature (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    expires_at TEXT NOT NULL,
    app_version TEXT NOT NULL,
    reported_at TEXT NOT NULL
) STRICT;
