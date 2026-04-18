# WO-31 — Wire event_bus to Worker for SSE Entity Push

## Summary

The UI serve command creates a `WorkerRunner` without passing the `event_bus`. The worker's `EntityExtractor` therefore has `event_bus=None`, meaning new entities never trigger SSE pushes to the feed. Cards only appear after the 3s polling fallback.

## Files to Modify

- `src/llm_mem/cli.py` — pass `event_bus` to `WorkerRunner` in the serve path
- `src/llm_mem/core/workers.py` — accept and forward `event_bus` to handlers
- `src/llm_mem/mcp/server.py` — pass `event_bus` if available

## Approach

1. Add `event_bus` parameter to `WorkerRunner.__init__()` (default `None`)
2. When constructing `EntityExtractor` in `register_handlers()`, pass the `event_bus`
3. In `cli.py` serve command, pass the app's `EventBus` to `WorkerRunner`

## Acceptance Criteria

- [ ] `WorkerRunner` accepts optional `event_bus`
- [ ] `EntityExtractor` in worker gets `event_bus` in serve mode
- [ ] New entities trigger SSE `entity_created` events
- [ ] Tests pass
