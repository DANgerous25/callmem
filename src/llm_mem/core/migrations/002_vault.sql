-- llm-mem schema v2: Vault table and scan_status column

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
