# WO-18: UI Polish — Project Filtering, Infinite Scroll, Token Economics in Feed

## Priority: P2

## Objective

Polish the web UI with project-level filtering, infinite scroll pagination, and token economics display in the feed — closing the remaining visual/UX gaps with claude-mem.

---

## 1. Project Filtering in Feed

### Why

When running llm-mem across multiple projects, the feed shows everything mixed together. Users need to filter to one project at a time, like claude-mem's "All Projects" dropdown.

### Implementation

#### Project selector in feed header

Add a dropdown/pill bar above the feed:

```html
<div class="feed-filters">
  <button class="filter-pill filter-active" hx-get="/partials/feed" hx-vals='{"project": ""}'>All</button>
  {% for p in projects %}
  <button class="filter-pill" hx-get="/partials/feed" hx-vals='{"project": "{{ p.id }}"}'>{{ p.name }}</button>
  {% endfor %}
</div>
```

Clicking a pill reloads the feed filtered to that project. Active pill gets highlighted style.

#### Backend changes

- `GET /partials/feed?project=<id>` — filter entities/sessions by project_id
- `_build_feed_items()` in `feed.py` — accept optional `project_id` parameter
- `engine.get_entities(project_id=...)` — pass through filter

#### Project list

Query distinct project_ids from the `entities` or `sessions` table. Map to display names from config or metadata.

### Files to Modify

- `src/llm_mem/ui/routes/feed.py` — accept `project` query param, pass to feed builder
- `src/llm_mem/ui/templates/feed.html` — project filter pill bar
- `src/llm_mem/ui/templates/feed_partial.html` — no change (already dynamic)
- `src/llm_mem/ui/templates/base.html` — CSS for filter pills

---

## 2. Infinite Scroll / Pagination

### Why

The feed currently loads up to 100 entities. As the database grows, this will become slow and the page will be very long. Infinite scroll loads items progressively as the user scrolls down.

### Implementation

#### htmx infinite scroll pattern

```html
<!-- Last card in the feed includes a trigger to load more -->
{% if has_more %}
<div hx-get="/partials/feed?offset={{ next_offset }}&project={{ project }}"
     hx-trigger="revealed"
     hx-swap="afterend"
     class="feed-loader">
  Loading more...
</div>
{% endif %}
```

When the loader div enters the viewport, htmx fetches the next page and appends cards below.

#### Backend changes

- `_build_feed_items()` accepts `offset` and `limit` (default 30) parameters
- Returns `has_more` flag and `next_offset` value
- Template receives these for the infinite scroll trigger

#### Deduplication

When appending new items via infinite scroll, entity IDs already in the DOM should be skipped. Use `hx-swap="afterend"` (append, not replace) and include item IDs as `data-entity-id` attributes for client-side dedup if needed.

### Files to Modify

- `src/llm_mem/ui/routes/feed.py` — pagination params (offset, limit), has_more flag
- `src/llm_mem/ui/templates/feed_partial.html` — infinite scroll trigger at bottom

---

## 3. Token Economics in Feed

### Why

claude-mem shows "Read" and "Work" token costs per observation. This helps users understand the value: "I spent 500k tokens of work and it's compressed into 15k tokens of context."

### Implementation

#### Per-card token display

In the feed card footer, show token count:

```html
<span class="feed-card-tokens" title="Tokens to read this entity">
  {{ item.token_count }}t
</span>
```

Where `token_count` is estimated from the entity's content length (`len(content) / 4`).

#### Feed header economics

At the top of the feed (in the stats bar), show aggregate economics:

```html
<p>
  Events: {{ event_count }} &middot;
  Entities: {{ entity_count }} &middot;
  Sessions: {{ session_count }} &middot;
  <strong>{{ total_work_tokens | format_number }}t captured → {{ total_briefing_tokens | format_number }}t briefing ({{ savings_pct }}% savings)</strong>
</p>
```

#### Backend changes

- Compute `total_work_tokens`: `SELECT SUM(token_count) FROM events WHERE project_id = ?`
- Compute `total_briefing_tokens`: current briefing token count
- Compute `savings_pct`: `1 - (briefing / work) * 100`
- Pass to template context

### Files to Modify

- `src/llm_mem/ui/routes/feed.py` — compute economics, pass to template
- `src/llm_mem/ui/templates/feed.html` — display economics in header
- `src/llm_mem/ui/templates/feed_partial.html` — token count in card footer
- `src/llm_mem/ui/templates/base.html` — CSS for token display (muted, small text)

---

## Acceptance Criteria

### Project Filtering
1. [ ] Project filter pills shown above the feed
2. [ ] "All" pill shows all projects (default)
3. [ ] Clicking a project pill filters the feed to that project only
4. [ ] Active pill is visually highlighted
5. [ ] Filter state preserved during SSE/polling updates

### Infinite Scroll
6. [ ] Feed loads first 30 items on page load
7. [ ] Scrolling to bottom triggers automatic loading of next 30 items
8. [ ] "Loading more..." indicator shown during fetch
9. [ ] No duplicate cards on append
10. [ ] Works correctly with project filter active

### Token Economics
11. [ ] Per-card token count shown in feed card footer
12. [ ] Feed header shows: total work tokens, briefing tokens, savings percentage
13. [ ] Numbers formatted with commas (e.g., "385,000t")

### General
14. [ ] All existing tests pass, new tests for pagination and filtering
15. [ ] `make lint` clean, `make test` all pass
16. [ ] Committed and pushed
