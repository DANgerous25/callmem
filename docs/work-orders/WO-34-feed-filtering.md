# WO-34 — Feed Filtering, Search, and Ordering

## Summary

The feed has no working filters. Project filter pills are dead code. No type filtering, no text search, no ordering control. Wire the partials endpoint to accept query params for type, search, ordering, and project.

## Files to Modify

- `src/callmem/ui/routes/feed.py` — accept and use filter parameters
- `src/callmem/ui/templates/feed.html` — add type filter pills, search input, order toggle
- `src/callmem/ui/templates/feed_partial.html` — no changes (items already have type badges)
- `src/callmem/core/engine.py` — support type list filtering in `get_entities()`

## Approach

### Type filter pills
Add a row of clickable type pills (All, Decision, Todo, Fact, Failure, Discovery, Feature, Bugfix, Research, Change) above the feed. Clicking one sends `hx-get="/partials/feed?type=decision"` etc.

### Text search
Add a search input that sends `hx-get="/partials/feed?q=searchterm"`. Uses existing FTS5 search.

### Ordering
Add asc/desc toggle button. Default: desc (newest first). Sends `?order=asc` or `?order=desc`.

### Route changes
`/partials/feed` reads `request.query_params` for `type`, `q`, `order`, `project`. Filters accordingly. Pass `projects` list to feed template for project pills.

## Acceptance Criteria

- [ ] Type filter pills work (clicking filters to that entity type)
- [ ] Text search input filters feed items
- [ ] Order toggle switches asc/desc
- [ ] All filters combine (type + search + order)
- [ ] `projects` passed to template so project pills render
- [ ] Tests pass
