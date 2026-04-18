# WO-33 — Queue Status Indicator

## Summary

Add a live-updating queue status badge in the UI header showing pending/processing/failed job counts, updated via SSE.

## Files to Modify

- `src/llm_mem/core/workers.py` — publish queue events after job complete/fail
- `src/llm_mem/ui/templates/base.html` — add queue badge in nav header
- `src/llm_mem/ui/routes/sse.py` — no changes needed (EventBus already relays)
- `src/llm_mem/ui/templates/feed.html` — add JS listener for queue events

## Approach

1. After `process_one()` completes or fails a job, publish `queue_updated` event via event_bus with current counts
2. Add a badge element in the header nav: `<span id="queue-badge">0</span>`
3. Add SSE listener in base.html JS that updates the badge on `queue_updated` events
4. Add a `/api/queue-status` endpoint for initial load (before any SSE events arrive)

## Acceptance Criteria

- [ ] Queue badge visible in header
- [ ] Updates live via SSE when jobs complete/fail
- [ ] Shows pending count (and processing/failed if non-zero)
- [ ] Tests pass
