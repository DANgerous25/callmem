-- callmem schema v17: full provenance for extracted entities
--
-- source_event_ids: JSON array of every event id an entity was extracted
-- from (a batch may cover many events). source_event_id keeps the first
-- id for backward compatibility with existing joins/queries.
--
-- Existing rows have source_event_ids = NULL; consumers (compaction
-- protection, re-extraction archive-by-source-event, briefing session
-- grouping) must fall back to source_event_id for those rows.

ALTER TABLE entities ADD COLUMN source_event_ids TEXT;
