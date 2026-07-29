-- callmem schema v19: per-entity citation persistence
--
-- cited_count: number of times this entity's short ID (#XXXXXXXX) has
-- been detected in an agent response, per the citation-detection logic
-- in usage.py. last_cited_at: timestamp of the most recent citation.
--
-- Both feed the briefing's importance-ranked entity selection (see
-- BriefingGenerator._score_entity) — a citation is the strongest signal
-- that an entity mattered to a real session, so cited entities rank
-- higher and decay more slowly than uncited ones.
--
-- Existing rows backfill cited_count = 0 / last_cited_at = NULL, which is
-- indistinguishable from "never cited" — behaviour for pre-existing data
-- is no worse than before this column existed.

ALTER TABLE entities ADD COLUMN cited_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE entities ADD COLUMN last_cited_at TEXT;
