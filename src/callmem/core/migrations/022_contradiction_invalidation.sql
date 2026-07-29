-- callmem schema v22: consolidation CONTRADICTS verdict
--
-- Adds `invalidated_at` to entities so `callmem stale` can list
-- contradiction-invalidated entries distinctly from other staleness
-- reasons (superseded/manual/etc, which never stamp this column).
-- Reuses the existing stale / superseded_by / staleness_reason verbs
-- from schema v7 -- CONTRADICTS just additionally timestamps the
-- moment the existing entity was invalidated.
--
-- Adds `contradicted` to consolidation_log alongside added/updated/noop,
-- mirroring schema v21's per-run counters.

ALTER TABLE entities ADD COLUMN invalidated_at TEXT;
ALTER TABLE consolidation_log ADD COLUMN contradicted INTEGER DEFAULT 0;
