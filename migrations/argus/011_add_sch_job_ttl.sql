ALTER TABLE sch_job ADD COLUMN ttl_s REAL CHECK (ttl_s > 0);
