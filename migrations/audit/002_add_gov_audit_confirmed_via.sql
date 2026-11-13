ALTER TABLE gov_audit ADD COLUMN confirmed_via TEXT CHECK (confirmed_via IN ('voice', 'card'));
