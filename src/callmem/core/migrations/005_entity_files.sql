-- v5: File-level observation tracking

CREATE TABLE IF NOT EXISTS entity_files (
    entity_id TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    file_path TEXT NOT NULL,
    relation TEXT NOT NULL DEFAULT 'related',
    PRIMARY KEY (entity_id, file_path)
);
CREATE INDEX IF NOT EXISTS idx_entity_files_path ON entity_files(file_path);
