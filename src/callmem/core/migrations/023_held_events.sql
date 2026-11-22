-- callmem schema v23: events held back for the sensitive-data pre-scan
--
-- When a dedicated pre-scanner is configured and a scan fails (scanner
-- down, bad response), the event is parked here instead of in `events`,
-- so no reader -- extraction, summaries, briefing -- can pick it up and
-- send unscanned text to the main LLM backend. `event_json` is the
-- pattern-redacted event; `vault_entries` holds the encrypted pattern
-- hits, inserted into `vault` once the event is released.

CREATE TABLE IF NOT EXISTS held_events (
    id             TEXT PRIMARY KEY,
    project_id     TEXT NOT NULL,
    session_id     TEXT NOT NULL,
    event_json     TEXT NOT NULL,
    vault_entries  TEXT NOT NULL DEFAULT '[]',
    attempts       INTEGER NOT NULL DEFAULT 0,
    last_error     TEXT,
    held_at        TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_held_events_held_at ON held_events(held_at);
