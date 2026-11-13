CREATE TABLE pro_dedupe (
    trigger_id TEXT NOT NULL,
    dedupe_key TEXT NOT NULL,
    seen_at TEXT NOT NULL,
    PRIMARY KEY (trigger_id, dedupe_key)
) STRICT, WITHOUT ROWID;
