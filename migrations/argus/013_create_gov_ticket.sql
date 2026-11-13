CREATE TABLE gov_ticket (
    id TEXT PRIMARY KEY,
    tool TEXT NOT NULL,
    permission TEXT NOT NULL CHECK (permission IN ('send', 'critical')),
    args_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT
) STRICT;
