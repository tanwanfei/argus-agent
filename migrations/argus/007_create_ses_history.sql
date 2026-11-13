CREATE TABLE ses_history (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    trace_id TEXT NOT NULL,
    utterance TEXT NOT NULL,
    reply TEXT NOT NULL,
    via TEXT NOT NULL CHECK (via IN ('voice', 'typed'))
) STRICT;
