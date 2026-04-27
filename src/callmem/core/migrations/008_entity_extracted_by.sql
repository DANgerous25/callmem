-- v8: Track which LLM extracted each entity
--
-- The UI's "model" pill historically showed sessions.model_name,
-- which is the *coding agent* model (Claude Code / OpenCode). For
-- entities, users want to see which model performed the extraction
-- itself — so we store that on the entity row.

ALTER TABLE entities ADD COLUMN extracted_by TEXT;
