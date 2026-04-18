-- llm-mem schema v2: Vault table and scan_status column
--
-- Note: events.scan_status is currently unused by application code.
-- The engine stores scan_status inside the event metadata JSON dict
-- instead. The column is retained for potential future use as a
-- directly queryable index. See DECISIONS.md #011.

CREATE TABLE IF NOT EXISTS vault (
    id             TEXT PRIMARY KEY,
    project_id     TEXT NOT NULL REFERENCES projects(id),
    category       TEXT NOT NULL,
    detector       TEXT NOT NULL,
    pattern_name   TEXT,
    ciphertext     BLOB NOT NULL,
    created_at     TEXT NOT NULL,
    event_id       TEXT REFERENCES events(id),
    reviewed       INTEGER DEFAULT 0,
    false_positive INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_vault_project ON vault(project_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_vault_event ON vault(event_id);

ALTER TABLE events ADD COLUMN scan_status TEXT DEFAULT NULL;
