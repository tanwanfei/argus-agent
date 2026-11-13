ALTER TABLE obs_turn ADD COLUMN route TEXT CHECK (route IN ('rule', 'control', 'cloud', 'defer'));
