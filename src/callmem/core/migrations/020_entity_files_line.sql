-- callmem schema v20: line-number anchors for entity_files
--
-- entity_files previously recorded only a bare file_path per entity,
-- populated from the LLM's freeform "files" list. line_number lets
-- extraction record a precise anchor ("src/foo.py:123") parsed
-- deterministically from entity content (see core/anchors.py), so
-- briefing render and mem_get_entities/mem_search can validate
-- citations against the live working tree.
--
-- NULL means "file-level only, no specific line" — true for every
-- pre-existing row and for LLM-derived file mentions that don't carry
-- a line number.

ALTER TABLE entity_files ADD COLUMN line_number INTEGER;
