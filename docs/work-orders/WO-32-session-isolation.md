# WO-32 — Session Isolation for Imports

## Summary

When importing sessions, `engine.start_session()` creates an active session. If a live event arrives during import, `_ensure_active_session()` returns the import's session — live events get attached to the wrong session.

## Files to Modify

- `src/callmem/core/engine.py` — add mechanism to mark sessions as "import" sessions
- `src/callmem/adapters/opencode_import.py` — use isolated session scope

## Approach

Add a `_session_context` dict to the engine that tags the current session with a context (e.g., "import" vs "live"). `_ensure_active_session()` should only return sessions matching the current context.

Alternative (simpler): Import uses `engine.start_session()` / `engine.end_session()` directly and never relies on `_ensure_active_session()`. The import already does this — it calls `start_session()` explicitly. The issue is that `_ensure_active_session()` called by `ingest()` would find the import's session. Fix: add a `session_id` parameter to `ingest()` that overrides `_ensure_active_session()`.

## Acceptance Criteria

- [ ] Live events during import go to a different session (or no session)
- [ ] Import events stay in their designated session
- [ ] Existing tests pass
