CREATE TABLE llm_budget_extra (
    month TEXT PRIMARY KEY,
    extra_yuan REAL NOT NULL CHECK (extra_yuan > 0),
    updated_at TEXT NOT NULL
) STRICT;
