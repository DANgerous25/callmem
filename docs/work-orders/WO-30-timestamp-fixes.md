# WO-30 — Timestamp Fixes

## Summary

Three timestamp problems:
1. `EventInput` has no `timestamp` field, so imported events always get "now" instead of original times
2. UI displays raw UTC with no timezone conversion or relative formatting
3. Python and SQLite timestamp formats are inconsistent (`+00:00` suffix, microseconds)

## Files to Modify

- `src/llm_mem/models/events.py` — add optional `timestamp` to `EventInput`
- `src/llm_mem/core/engine.py` — use `EventInput.timestamp` if provided, else default
- `src/llm_mem/adapters/opencode_import.py` — pass original timestamps through `EventInput`
- `src/llm_mem/ui/templates/feed_partial.html` — relative time formatting
- `src/llm_mem/ui/templates/feed.html` — relative time in SSE prepend JS
- `src/llm_mem/ui/templates/session_detail.html` — local time display

## Approach

### EventInput timestamp
Add optional `timestamp: str | None = None` to `EventInput`. In `_create_event()`, if `event_input.timestamp` is set, use it; otherwise let the model default to `datetime.now(UTC)`.

### Import timestamps
In `_map_message()`, include `ts_iso` in the returned `EventInput.metadata` or as the `timestamp` field directly.

### UI formatting
Add a Jinja2 filter or template macro that converts ISO timestamps to:
- Relative time for recent items (< 24h): "2 hours ago", "5 minutes ago"
- Local date+time for older items: "Apr 17, 2:30 PM"
- Client-side JS for SSE prepend items

## Acceptance Criteria

- [ ] `EventInput` accepts optional `timestamp`
- [ ] `_create_event()` preserves passed timestamp
- [ ] Import passes original timestamps
- [ ] UI shows relative/local times, not raw UTC
- [ ] Existing tests pass
