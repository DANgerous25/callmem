-- callmem schema v9: Task graph support (A1)
--
-- Structured task tree with parent/child hierarchy, execution state,
-- model assignment tracking, eval scoring, and cost/token accounting.
-- Any multi-step agent workflow can declare, track, and resolve tasks
-- that survive context resets.

CREATE TABLE IF NOT EXISTS tasks (
    id              TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL REFERENCES projects(id),
    parent_id       TEXT REFERENCES tasks(id),
    session_id      TEXT REFERENCES sessions(id),
    title           TEXT NOT NULL,
    description     TEXT,
    status          TEXT NOT NULL DEFAULT 'pending',
    model_assigned  TEXT,
    model_reason    TEXT,
    eval_score      REAL,
    eval_feedback   TEXT,
    cost_usd        REAL DEFAULT 0,
    tokens_input    INTEGER DEFAULT 0,
    tokens_output   INTEGER DEFAULT 0,
    result_ref      TEXT,
    task_type       TEXT,
    complexity_hint INTEGER,
    retry_count     INTEGER DEFAULT 0,
    retry_of        TEXT REFERENCES tasks(id),
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    completed_at    TEXT,
    metadata        TEXT
);

CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks(parent_id);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status) WHERE status IN ('pending', 'in_progress');
CREATE INDEX IF NOT EXISTS idx_tasks_session ON tasks(session_id);
