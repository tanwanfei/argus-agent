CREATE TABLE gov_audit (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    trace_id TEXT,
    call_id TEXT NOT NULL,
    origin TEXT NOT NULL,
    speaker TEXT NOT NULL,
    tool TEXT NOT NULL,
    args_json TEXT NOT NULL,
    decision TEXT NOT NULL,
    deny_reason TEXT,
    result TEXT,
    duration_ms INTEGER
) STRICT;
CREATE TRIGGER gov_audit_no_update BEFORE UPDATE ON gov_audit
BEGIN
    SELECT RAISE(ABORT, 'gov_audit 只追加，不能改');
END;
CREATE TRIGGER gov_audit_no_delete BEFORE DELETE ON gov_audit
BEGIN
    SELECT RAISE(ABORT, 'gov_audit 只追加，不能删');
END;
