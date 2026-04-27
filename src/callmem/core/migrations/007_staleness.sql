-- callmem schema v7: entity staleness detection
--
-- Adds fields so outdated entities can be suppressed from briefings
-- and search results without deleting them. `stale` is the main flag
-- used by read paths; `superseded_by` and `staleness_reason` are
-- diagnostic metadata populated by the automatic detector and
-- by manual `mem_mark_stale` calls.

ALTER TABLE entities ADD COLUMN stale INTEGER NOT NULL DEFAULT 0;
ALTER TABLE entities ADD COLUMN superseded_by TEXT;
ALTER TABLE entities ADD COLUMN staleness_reason TEXT;

-- Index so the default read path (exclude stale) stays cheap.
CREATE INDEX IF NOT EXISTS idx_entities_stale
    ON entities(project_id, stale, type, updated_at DESC);
