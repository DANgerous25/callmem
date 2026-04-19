-- callmem schema v1: Core tables, FTS5 indexes, and triggers

-- Projects
CREATE TABLE IF NOT EXISTS projects (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    root_path   TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    metadata    TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_projects_name ON projects(name);

-- Sessions
CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL REFERENCES projects(id),
    started_at  TEXT NOT NULL,
    ended_at    TEXT,
    status      TEXT NOT NULL DEFAULT 'active',
    agent_name  TEXT,
    model_name  TEXT,
    summary     TEXT,
    event_count INTEGER DEFAULT 0,
    metadata    TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status);

-- Events
CREATE TABLE IF NOT EXISTS events (
    id          TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL REFERENCES sessions(id),
    project_id  TEXT NOT NULL REFERENCES projects(id),
    type        TEXT NOT NULL,
    content     TEXT NOT NULL,
    timestamp   TEXT NOT NULL,
    token_count INTEGER,
    metadata    TEXT,
    archived_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_events_project_time ON events(project_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(type, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_events_archived ON events(archived_at) WHERE archived_at IS NULL;

-- Entities
CREATE TABLE IF NOT EXISTS entities (
    id              TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL REFERENCES projects(id),
    source_event_id TEXT REFERENCES events(id),
    type            TEXT NOT NULL,
    title           TEXT NOT NULL,
    content         TEXT NOT NULL,
    status          TEXT,
    priority        TEXT,
    pinned          INTEGER DEFAULT 0,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    resolved_at     TEXT,
    metadata        TEXT,
    archived_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_entities_project_type ON entities(project_id, type, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_entities_status ON entities(type, status) WHERE status IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_entities_pinned ON entities(project_id, pinned) WHERE pinned = 1;
CREATE INDEX IF NOT EXISTS idx_entities_archived ON entities(archived_at) WHERE archived_at IS NULL;

-- Summaries
CREATE TABLE IF NOT EXISTS summaries (
    id                TEXT PRIMARY KEY,
    project_id        TEXT NOT NULL REFERENCES projects(id),
    session_id        TEXT REFERENCES sessions(id),
    level             TEXT NOT NULL,
    content           TEXT NOT NULL,
    event_range_start TEXT,
    event_range_end   TEXT,
    event_count       INTEGER,
    token_count       INTEGER,
    created_at        TEXT NOT NULL,
    metadata          TEXT
);
CREATE INDEX IF NOT EXISTS idx_summaries_project_level ON summaries(project_id, level, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_summaries_session ON summaries(session_id, level);

-- Memory edges
CREATE TABLE IF NOT EXISTS memory_edges (
    id          TEXT PRIMARY KEY,
    source_id   TEXT NOT NULL,
    source_type TEXT NOT NULL,
    target_id   TEXT NOT NULL,
    target_type TEXT NOT NULL,
    relation    TEXT NOT NULL,
    weight      REAL DEFAULT 1.0,
    created_at  TEXT NOT NULL,
    metadata    TEXT
);
CREATE INDEX IF NOT EXISTS idx_edges_source ON memory_edges(source_id, source_type);
CREATE INDEX IF NOT EXISTS idx_edges_target ON memory_edges(target_id, target_type);
CREATE INDEX IF NOT EXISTS idx_edges_relation ON memory_edges(relation);

-- Compaction log
CREATE TABLE IF NOT EXISTS compaction_log (
    id                TEXT PRIMARY KEY,
    project_id        TEXT NOT NULL REFERENCES projects(id),
    run_at            TEXT NOT NULL,
    events_archived   INTEGER DEFAULT 0,
    summaries_created INTEGER DEFAULT 0,
    entities_merged   INTEGER DEFAULT 0,
    duration_ms       INTEGER,
    policy_config     TEXT,
    metadata          TEXT
);
CREATE INDEX IF NOT EXISTS idx_compaction_project ON compaction_log(project_id, run_at DESC);

-- Config (per-project key-value)
CREATE TABLE IF NOT EXISTS config (
    project_id  TEXT NOT NULL REFERENCES projects(id),
    key         TEXT NOT NULL,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (project_id, key)
);

-- Job queue
CREATE TABLE IF NOT EXISTS jobs (
    id           TEXT PRIMARY KEY,
    type         TEXT NOT NULL,
    payload      TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',
    attempts     INTEGER DEFAULT 0,
    max_attempts INTEGER DEFAULT 3,
    created_at   TEXT NOT NULL,
    started_at   TEXT,
    completed_at TEXT,
    error        TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, created_at);

-- ============================================================
-- FTS5 virtual tables
-- ============================================================

-- Events FTS
CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(
    content,
    content='events',
    content_rowid='rowid',
    tokenize='porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS events_ai AFTER INSERT ON events BEGIN
    INSERT INTO events_fts(rowid, content) VALUES (new.rowid, new.content);
END;

CREATE TRIGGER IF NOT EXISTS events_ad AFTER DELETE ON events BEGIN
    INSERT INTO events_fts(events_fts, rowid, content) VALUES('delete', old.rowid, old.content);
END;

CREATE TRIGGER IF NOT EXISTS events_au AFTER UPDATE ON events BEGIN
    INSERT INTO events_fts(events_fts, rowid, content) VALUES('delete', old.rowid, old.content);
    INSERT INTO events_fts(rowid, content) VALUES (new.rowid, new.content);
END;

-- Entities FTS
CREATE VIRTUAL TABLE IF NOT EXISTS entities_fts USING fts5(
    title,
    content,
    content='entities',
    content_rowid='rowid',
    tokenize='porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS entities_ai AFTER INSERT ON entities BEGIN
    INSERT INTO entities_fts(rowid, title, content) VALUES (new.rowid, new.title, new.content);
END;

CREATE TRIGGER IF NOT EXISTS entities_ad AFTER DELETE ON entities BEGIN
    INSERT INTO entities_fts(entities_fts, rowid, title, content) VALUES('delete', old.rowid, old.title, old.content);
END;

CREATE TRIGGER IF NOT EXISTS entities_au AFTER UPDATE ON entities BEGIN
    INSERT INTO entities_fts(entities_fts, rowid, title, content) VALUES('delete', old.rowid, old.title, old.content);
    INSERT INTO entities_fts(rowid, title, content) VALUES (new.rowid, new.title, new.content);
END;

-- Summaries FTS
CREATE VIRTUAL TABLE IF NOT EXISTS summaries_fts USING fts5(
    content,
    content='summaries',
    content_rowid='rowid',
    tokenize='porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS summaries_ai AFTER INSERT ON summaries BEGIN
    INSERT INTO summaries_fts(rowid, content) VALUES (new.rowid, new.content);
END;

CREATE TRIGGER IF NOT EXISTS summaries_ad AFTER DELETE ON summaries BEGIN
    INSERT INTO summaries_fts(summaries_fts, rowid, content) VALUES('delete', old.rowid, old.content);
END;

CREATE TRIGGER IF NOT EXISTS summaries_au AFTER UPDATE ON summaries BEGIN
    INSERT INTO summaries_fts(summaries_fts, rowid, content) VALUES('delete', old.rowid, old.content);
    INSERT INTO summaries_fts(rowid, content) VALUES (new.rowid, new.content);
END;
