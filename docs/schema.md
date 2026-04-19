# SQLite Schema

## Design principles

1. **Normalize for query flexibility, denormalize for read performance** — structured entities get their own tables; FTS5 indexes provide fast text search
2. **Every row has a stable ID** — ULIDs for time-sortable unique IDs
3. **Soft deletes** — `archived_at` column instead of `DELETE`, so compaction is reversible
4. **Schema versioning** — `schema_version` table for safe migrations
5. **Embeddings-ready** — optional `embeddings` table with the same foreign keys, added in v2

## Tables

### `projects`

Top-level grouping. One callmem instance can serve multiple projects.

```sql
CREATE TABLE projects (
    id          TEXT PRIMARY KEY,  -- ULID
    name        TEXT NOT NULL,
    root_path   TEXT,              -- Absolute path to project directory
    created_at  TEXT NOT NULL,     -- ISO 8601
    updated_at  TEXT NOT NULL,
    metadata    TEXT               -- JSON blob for extensibility
);
CREATE UNIQUE INDEX idx_projects_name ON projects(name);
```

### `sessions`

A continuous period of agent interaction.

```sql
CREATE TABLE sessions (
    id          TEXT PRIMARY KEY,  -- ULID
    project_id  TEXT NOT NULL REFERENCES projects(id),
    started_at  TEXT NOT NULL,     -- ISO 8601
    ended_at    TEXT,              -- NULL if still active
    status      TEXT NOT NULL DEFAULT 'active',  -- active, ended, abandoned
    agent_name  TEXT,              -- e.g. "opencode", "kilo", "claude-code"
    model_name  TEXT,              -- Interactive model used
    summary     TEXT,              -- Session-level summary (generated)
    event_count INTEGER DEFAULT 0,
    metadata    TEXT               -- JSON
);
CREATE INDEX idx_sessions_project ON sessions(project_id, started_at DESC);
CREATE INDEX idx_sessions_status ON sessions(status);
```

### `events`

Raw events ingested during a session. The foundational data layer.

```sql
CREATE TABLE events (
    id          TEXT PRIMARY KEY,  -- ULID
    session_id  TEXT NOT NULL REFERENCES sessions(id),
    project_id  TEXT NOT NULL REFERENCES projects(id),
    type        TEXT NOT NULL,     -- prompt, response, tool_call, file_change, user_note
    content     TEXT NOT NULL,     -- Raw content
    timestamp   TEXT NOT NULL,     -- ISO 8601
    token_count INTEGER,           -- Estimated tokens in content
    metadata    TEXT,              -- JSON: {model, tool_name, file_path, diff_summary, ...}
    archived_at TEXT               -- Set by compaction; NULL = active
);
CREATE INDEX idx_events_session ON events(session_id, timestamp);
CREATE INDEX idx_events_project_time ON events(project_id, timestamp DESC);
CREATE INDEX idx_events_type ON events(type, timestamp DESC);
CREATE INDEX idx_events_archived ON events(archived_at) WHERE archived_at IS NULL;
```

### `entities`

Structured knowledge extracted from events by the memory-maintenance LLM.

```sql
CREATE TABLE entities (
    id          TEXT PRIMARY KEY,  -- ULID
    project_id  TEXT NOT NULL REFERENCES projects(id),
    source_event_id TEXT REFERENCES events(id),  -- Which event this was extracted from
    type        TEXT NOT NULL,     -- decision, todo, fact, failure, discovery
    title       TEXT NOT NULL,     -- Short label
    content     TEXT NOT NULL,     -- Full description
    status      TEXT,              -- For TODOs: open, done, cancelled. For failures: unresolved, resolved
    priority    TEXT,              -- high, medium, low
    pinned      INTEGER DEFAULT 0, -- 1 = user-pinned, never compacted
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    resolved_at TEXT,              -- When a TODO was completed or failure resolved
    metadata    TEXT,              -- JSON: {related_files, tags, ...}
    archived_at TEXT
);
CREATE INDEX idx_entities_project_type ON entities(project_id, type, created_at DESC);
CREATE INDEX idx_entities_status ON entities(type, status) WHERE status IS NOT NULL;
CREATE INDEX idx_entities_pinned ON entities(project_id, pinned) WHERE pinned = 1;
CREATE INDEX idx_entities_archived ON entities(archived_at) WHERE archived_at IS NULL;
```

### `summaries`

Layered summaries at different granularities.

```sql
CREATE TABLE summaries (
    id          TEXT PRIMARY KEY,  -- ULID
    project_id  TEXT NOT NULL REFERENCES projects(id),
    session_id  TEXT REFERENCES sessions(id),  -- NULL for cross-session summaries
    level       TEXT NOT NULL,     -- chunk, session, cross_session
    content     TEXT NOT NULL,     -- Summary text
    event_range_start TEXT,        -- First event ID covered
    event_range_end   TEXT,        -- Last event ID covered
    event_count INTEGER,           -- Number of events summarized
    token_count INTEGER,           -- Tokens in this summary
    created_at  TEXT NOT NULL,
    metadata    TEXT               -- JSON
);
CREATE INDEX idx_summaries_project_level ON summaries(project_id, level, created_at DESC);
CREATE INDEX idx_summaries_session ON summaries(session_id, level);
```

### `memory_edges`

Links between entities, events, and summaries. Models relationships like "decision X was made because of failure Y" or "TODO A relates to file B".

```sql
CREATE TABLE memory_edges (
    id          TEXT PRIMARY KEY,  -- ULID
    source_id   TEXT NOT NULL,     -- ID of source entity/event
    source_type TEXT NOT NULL,     -- entity, event, summary
    target_id   TEXT NOT NULL,     -- ID of target entity/event
    target_type TEXT NOT NULL,     -- entity, event, summary
    relation    TEXT NOT NULL,     -- caused_by, relates_to, supersedes, resolves, blocks
    weight      REAL DEFAULT 1.0,  -- Relation strength
    created_at  TEXT NOT NULL,
    metadata    TEXT
);
CREATE INDEX idx_edges_source ON memory_edges(source_id, source_type);
CREATE INDEX idx_edges_target ON memory_edges(target_id, target_type);
CREATE INDEX idx_edges_relation ON memory_edges(relation);
```

### `compaction_log`

Audit trail for compaction operations.

```sql
CREATE TABLE compaction_log (
    id              TEXT PRIMARY KEY,  -- ULID
    project_id      TEXT NOT NULL REFERENCES projects(id),
    run_at          TEXT NOT NULL,
    events_archived INTEGER DEFAULT 0,
    summaries_created INTEGER DEFAULT 0,
    entities_merged INTEGER DEFAULT 0,
    duration_ms     INTEGER,
    policy_config   TEXT,              -- JSON snapshot of compaction config used
    metadata        TEXT
);
CREATE INDEX idx_compaction_project ON compaction_log(project_id, run_at DESC);
```

### `config`

Per-project configuration stored in the database (supplements file-based config).

```sql
CREATE TABLE config (
    project_id  TEXT NOT NULL REFERENCES projects(id),
    key         TEXT NOT NULL,
    value       TEXT NOT NULL,     -- JSON-encoded value
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (project_id, key)
);
```

### `schema_version`

Migration tracking.

```sql
CREATE TABLE schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT NOT NULL,
    description TEXT
);
```

### `embeddings` (v2 — optional)

Vector embeddings for semantic search. Not created in v1.

```sql
-- v2: Created only when embeddings backend is configured
CREATE TABLE embeddings (
    id          TEXT PRIMARY KEY,  -- ULID
    source_id   TEXT NOT NULL,     -- ID of the entity/event/summary being embedded
    source_type TEXT NOT NULL,     -- entity, event, summary
    model       TEXT NOT NULL,     -- Embedding model name
    vector      BLOB NOT NULL,     -- Raw float32 vector bytes
    dimensions  INTEGER NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX idx_embeddings_source ON embeddings(source_id, source_type);
CREATE INDEX idx_embeddings_model ON embeddings(model);
```

Note: For v2, consider using `sqlite-vec` extension for native vector operations, or keep vectors in a separate FAISS/hnswlib index with SQLite as the metadata store.

## FTS5 strategy

Two FTS5 virtual tables provide full-text search across events and entities.

### Events FTS

```sql
CREATE VIRTUAL TABLE events_fts USING fts5(
    content,
    content='events',
    content_rowid='rowid',
    tokenize='porter unicode61'
);

-- Triggers to keep FTS in sync
CREATE TRIGGER events_ai AFTER INSERT ON events BEGIN
    INSERT INTO events_fts(rowid, content) VALUES (new.rowid, new.content);
END;

CREATE TRIGGER events_ad AFTER DELETE ON events BEGIN
    INSERT INTO events_fts(events_fts, rowid, content) VALUES('delete', old.rowid, old.content);
END;

CREATE TRIGGER events_au AFTER UPDATE ON events BEGIN
    INSERT INTO events_fts(events_fts, rowid, content) VALUES('delete', old.rowid, old.content);
    INSERT INTO events_fts(rowid, content) VALUES (new.rowid, new.content);
END;
```

### Entities FTS

```sql
CREATE VIRTUAL TABLE entities_fts USING fts5(
    title,
    content,
    content='entities',
    content_rowid='rowid',
    tokenize='porter unicode61'
);

-- Same trigger pattern as events_fts
CREATE TRIGGER entities_ai AFTER INSERT ON entities BEGIN
    INSERT INTO entities_fts(rowid, title, content) VALUES (new.rowid, new.title, new.content);
END;

CREATE TRIGGER entities_ad AFTER DELETE ON entities BEGIN
    INSERT INTO entities_fts(entities_fts, rowid, title, content) VALUES('delete', old.rowid, old.title, old.content);
END;

CREATE TRIGGER entities_au AFTER UPDATE ON entities BEGIN
    INSERT INTO entities_fts(entities_fts, rowid, title, content) VALUES('delete', old.rowid, old.title, old.content);
    INSERT INTO entities_fts(rowid, title, content) VALUES (new.rowid, new.title, new.content);
END;
```

### Summaries FTS

```sql
CREATE VIRTUAL TABLE summaries_fts USING fts5(
    content,
    content='summaries',
    content_rowid='rowid',
    tokenize='porter unicode61'
);

-- Same trigger pattern
```

### FTS5 query examples

```sql
-- Simple keyword search
SELECT e.*, rank FROM events_fts
JOIN events e ON events_fts.rowid = e.rowid
WHERE events_fts MATCH 'pagination cursor'
ORDER BY rank;

-- Search with BM25 ranking
SELECT e.id, e.type, e.content, bm25(events_fts) as score
FROM events_fts
JOIN events e ON events_fts.rowid = e.rowid
WHERE events_fts MATCH 'authentication AND oauth'
ORDER BY score;

-- Prefix search
SELECT * FROM entities_fts WHERE entities_fts MATCH 'rate limit*';

-- Column-weighted search (title more important)
SELECT *, bm25(entities_fts, 10.0, 1.0) as score
FROM entities_fts
WHERE entities_fts MATCH 'redis cache'
ORDER BY score;
```

## Index strategy summary

| Query pattern | Index used |
|---|---|
| Events by session + time | `idx_events_session` |
| Events by project + recent | `idx_events_project_time` |
| Events by type | `idx_events_type` |
| Non-archived events | `idx_events_archived` (partial) |
| Entities by project + type | `idx_entities_project_type` |
| Active TODOs / unresolved failures | `idx_entities_status` (partial) |
| Pinned entities | `idx_entities_pinned` (partial) |
| Keyword search in events | `events_fts` (FTS5) |
| Keyword search in entities | `entities_fts` (FTS5) |
| Keyword search in summaries | `summaries_fts` (FTS5) |
| Memory graph traversal | `idx_edges_source`, `idx_edges_target` |

## ID generation

All IDs use [ULID](https://github.com/ulid/spec) — Universally Unique Lexicographically Sortable Identifiers. Benefits:
- Time-sortable: `ORDER BY id` is chronological
- 128-bit uniqueness: no collisions across projects
- Text-friendly: 26-character Crockford Base32 string
- Compatible with SQLite TEXT PRIMARY KEY

Python library: `python-ulid`

## Migration strategy

Schema changes are tracked in `schema_version`. The migration runner:
1. Reads current version from `schema_version`
2. Applies all pending migration scripts in order
3. Records each applied migration
4. Migration scripts live in `src/callmem/core/migrations/`

Migrations are plain SQL files named `NNN_description.sql` (e.g., `001_initial.sql`, `002_add_embeddings.sql`).
