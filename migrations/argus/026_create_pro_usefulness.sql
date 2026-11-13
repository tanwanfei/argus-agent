CREATE TABLE pro_sent (
    sent_at TEXT NOT NULL,
    trigger_id TEXT NOT NULL,
    verdict TEXT NOT NULL CHECK (verdict IN ('speak', 'notify'))
) STRICT;
CREATE TABLE pro_useless_said (
    said_at TEXT NOT NULL,
    trigger_id TEXT NOT NULL
) STRICT;
