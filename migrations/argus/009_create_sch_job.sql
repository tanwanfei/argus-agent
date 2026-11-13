CREATE TABLE sch_job (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    key TEXT,
    due_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'failed'))
) STRICT;
CREATE UNIQUE INDEX sch_job_pending_key ON sch_job (key) WHERE key IS NOT NULL AND status = 'pending';
