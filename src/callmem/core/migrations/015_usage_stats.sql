-- callmem schema v15: Usage stats tracking
--
-- Tracks calls to compile_context and file_context to report token
-- savings vs reading raw files. Each call records the estimated tokens
-- saved and a timestamp.

CREATE TABLE IF NOT EXISTS usage_stats (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  TEXT NOT NULL REFERENCES projects(id),
    tool_name   TEXT NOT NULL,
    tokens_saved INTEGER DEFAULT 0,
    called_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_usage_project
    ON usage_stats(project_id, called_at DESC);
CREATE INDEX IF NOT EXISTS idx_usage_tool
    ON usage_stats(tool_name);
