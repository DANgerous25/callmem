-- callmem schema v10: Model performance tracking (A2)
--
-- Aggregates per-model performance metrics, updated incrementally as
-- tasks complete. Enables answering "which model is best at coding
-- tasks?" and "how much have I spent on Sonnet vs Haiku?"

CREATE TABLE IF NOT EXISTS model_stats (
    id              TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL REFERENCES projects(id),
    model_name      TEXT NOT NULL,
    task_type       TEXT,
    tasks_completed INTEGER DEFAULT 0,
    tasks_failed    INTEGER DEFAULT 0,
    avg_eval_score  REAL,
    total_cost_usd  REAL DEFAULT 0,
    total_tokens_in INTEGER DEFAULT 0,
    total_tokens_out INTEGER DEFAULT 0,
    first_seen      TEXT NOT NULL,
    last_seen       TEXT NOT NULL,
    metadata        TEXT,
    UNIQUE(project_id, model_name, task_type)
);
