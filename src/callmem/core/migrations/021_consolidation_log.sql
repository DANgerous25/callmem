-- callmem schema v21: consolidation run metrics
--
-- One row per extraction-batch consolidation pass (see
-- callmem.core.consolidation.EntityConsolidator), mirroring compaction_log's
-- per-run metrics shape. Consolidation itself adds no columns to `entities`
-- -- it reuses the existing stale / superseded_by / staleness_reason /
-- archived_at verbs from schema v7 (staleness) and v1 (archival), so no
-- entity migration is needed here.

CREATE TABLE IF NOT EXISTS consolidation_log (
    id           TEXT PRIMARY KEY,
    project_id   TEXT NOT NULL REFERENCES projects(id),
    run_at       TEXT NOT NULL,
    added        INTEGER DEFAULT 0,
    updated      INTEGER DEFAULT 0,
    noop         INTEGER DEFAULT 0,
    judge_failed INTEGER DEFAULT 0,
    metadata     TEXT
);
CREATE INDEX IF NOT EXISTS idx_consolidation_project ON consolidation_log(project_id, run_at DESC);
