CREATE TABLE ses_message (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    trace_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    speaker TEXT NOT NULL,
    content TEXT NOT NULL
) STRICT;
