-- WO-19: Knowledge agents corpus tables

CREATE TABLE IF NOT EXISTS corpora (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    project_id TEXT,
    filters TEXT NOT NULL,
    entity_count INTEGER NOT NULL,
    token_count INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS corpus_entities (
    corpus_id TEXT NOT NULL REFERENCES corpora(id) ON DELETE CASCADE,
    entity_id TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    PRIMARY KEY (corpus_id, entity_id)
);
