CREATE TABLE code_task (
    id TEXT NOT NULL PRIMARY KEY,
    project TEXT NOT NULL,
    request TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running', 'finished', 'failed', 'published', 'removed')),
    created_at TEXT NOT NULL,
    workspace_kind TEXT NOT NULL CHECK (workspace_kind IN ('git', 'folder')),
    workspace_path TEXT NOT NULL,
    source_path TEXT NOT NULL,
    branch TEXT,
    base_commit TEXT,
    agent_session_id TEXT,
    finished_at TEXT,
    report_finished INTEGER CHECK (report_finished IN (0, 1)),
    report_turns INTEGER,
    summary TEXT,
    tests TEXT,
    changed_files INTEGER
) STRICT;
