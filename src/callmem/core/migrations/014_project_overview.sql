-- callmem schema v14: Project Overview — always-visible project summary
--
-- A single manually-authored summary per project that appears at the top
-- of every briefing, before the Context Economics section. Unlike
-- entities, the overview is never auto-extracted or touched by re-extract;
-- it is set explicitly via mem_set_overview (MCP) or `callmem overview set`
-- (CLI). One row per project — writing a new overview upserts over the old.

CREATE TABLE IF NOT EXISTS project_overview (
    project_id  TEXT PRIMARY KEY REFERENCES projects(id),
    content     TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    updated_by  TEXT
);

CREATE INDEX IF NOT EXISTS idx_project_overview
    ON project_overview(project_id);
