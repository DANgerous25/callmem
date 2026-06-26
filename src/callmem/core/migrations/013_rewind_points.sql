-- callmem schema v13: Undo / rewind support (A6)
--
-- Rewind points snapshot the memory state at a point in time so an
-- agent or user can roll back to a previous state. Restore soft-archives
-- everything created after the point (does not hard-delete).

CREATE TABLE IF NOT EXISTS rewind_points (
    id              TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL REFERENCES projects(id),
    label           TEXT,
    created_at      TEXT NOT NULL,
    event_count     INTEGER,
    entity_count    INTEGER,
    metadata        TEXT
);

CREATE INDEX IF NOT EXISTS idx_rewind_project ON rewind_points(project_id, created_at DESC);
