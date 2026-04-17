-- WO-13: Add key_points and synopsis to entities

ALTER TABLE entities ADD COLUMN key_points TEXT;
ALTER TABLE entities ADD COLUMN synopsis TEXT;
