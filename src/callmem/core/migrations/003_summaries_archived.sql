-- v3: Add archived_at to summaries for compaction

ALTER TABLE summaries ADD COLUMN archived_at TEXT;
