# WO-29 — Migration Cleanup: Dead scan_status Column

## Summary

Migration 002 adds `scan_status` column to the `events` table. However, the engine stores scan_status inside the `metadata` JSON dict, never writing to or reading from the dedicated column. This is a minor inconsistency that should be cleaned up.

## Files to Modify

- `src/callmem/core/engine.py` — write `scan_status` to the column instead of (or in addition to) metadata
- `src/callmem/core/repository.py` — update `insert_event` to accept and store `scan_status`
- `src/callmem/core/migrations/` — optionally add migration 007 to note the fix (or document in 002)

## Approach

Either:
1. **Use the column** — Update `insert_event` to accept `scan_status`, store it in the column, and remove from metadata. Update all callers.
2. **Document the decision** — Add a comment in migration 002 that scan_status is stored in metadata JSON, and the column exists for future direct-query use. No code changes.

Option 2 is simpler and lower risk. The column doesn't hurt anything.

## Acceptance Criteria

- [ ] Decision made and documented in DECISIONS.md
- [ ] If option 1: `insert_event` updated, tests updated, all tests pass
- [ ] If option 2: comment added to migration 002, DECISIONS.md updated
- [ ] `pytest tests/ -v` passes
