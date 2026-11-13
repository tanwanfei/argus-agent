CREATE TABLE pro_quota (
    day TEXT NOT NULL,
    verdict TEXT NOT NULL,
    used INTEGER NOT NULL,
    PRIMARY KEY (day, verdict)
) STRICT, WITHOUT ROWID;
