CREATE TABLE llm_cost (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    trace_id TEXT,
    purpose TEXT NOT NULL,
    tier TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    cached_input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cost_yuan REAL NOT NULL
) STRICT;
