CREATE TABLE pro_digest (
    id TEXT NOT NULL PRIMARY KEY,
    queued_at TEXT NOT NULL,
    trigger_id TEXT NOT NULL,
    text TEXT NOT NULL
) STRICT;
