CREATE TABLE mem_fact (
    id INTEGER PRIMARY KEY,
    subject TEXT NOT NULL,
    attribute TEXT NOT NULL,
    value TEXT NOT NULL,
    confidence REAL NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    source TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    superseded_by INTEGER,
    created_at TEXT NOT NULL
) STRICT;
CREATE INDEX mem_fact_current ON mem_fact (subject, attribute) WHERE superseded_by IS NULL;
CREATE TABLE mem_episode (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    summary TEXT NOT NULL,
    weight REAL NOT NULL DEFAULT 1.0 CHECK (weight >= 0),
    source_ref TEXT,
    created_at TEXT NOT NULL
) STRICT;
CREATE VIRTUAL TABLE mem_episode_vec USING vec0(embedding float[512] distance_metric=cosine);
CREATE TABLE mem_habit (
    id INTEGER PRIMARY KEY,
    condition_json TEXT NOT NULL,
    statement TEXT NOT NULL,
    support INTEGER NOT NULL CHECK (support >= 1),
    last_seen TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1))
) STRICT;
