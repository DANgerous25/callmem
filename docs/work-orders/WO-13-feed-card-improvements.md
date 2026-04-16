# WO-13: Feed Card UI Improvements — Expandable Cards, Key Points / Synopsis, Better Summaries

## Objective

Upgrade the feed card UI to match claude-mem's quality: expandable cards with two content views ("Key Points" and "Synopsis"), more meaningful default summaries, better entity categories, and full event text on the events page.

## Reference

See `image-2.jpg` and `image-3.jpg` in workspace — claude-mem's "facts" view shows bullet-point key points, "narrative" view shows flowing prose paragraph. Both toggle in-place on the same card. The card is collapsed by default showing just badges + title; clicking or toggling expands it.

---

## 1. Expandable Feed Cards

### Current behaviour
Cards show `content[:300]` truncated inline. No expand/collapse.

### Required behaviour
- Cards show **only** badges + title by default (collapsed state)
- Clicking the card title OR a chevron icon expands to show content
- Expanded content has two toggle buttons: **"Key Points"** and **"Synopsis"**
- Default expanded view = Key Points
- Expand/collapse uses htmx `hx-swap="innerHTML"` or plain JS (no page reload)
- Expanded state persists during htmx polling (don't collapse on 3s refresh)

### Files to modify
- `src/llm_mem/ui/templates/feed_partial.html` — card expand/collapse markup
- `src/llm_mem/ui/templates/base.html` — CSS for expanded state + JS for toggle logic

### Implementation notes
- Use a `<details>` element or a JS-toggled class (`feed-card-expanded`)
- htmx polling: the partial must preserve expanded state. Options:
  - Use `hx-swap="morph"` (htmx morphing extension) to preserve DOM state
  - Or use `<details>` which natively preserves open/close across innerHTML swaps if the `open` attribute is managed client-side
  - Or track expanded card IDs in a JS Set and re-expand after swap

---

## 2. Key Points and Synopsis Views

### What these are
Two content representations of the same entity, inspired by claude-mem's "facts" / "narrative" toggle:

- **Key Points** (our "facts"): Bullet-point list of the essential information. Compact. ~50-100 tokens. Example:
  ```
  • Admin invite dialog was generating invite URLs with hardcoded `localhost` instead of `window.location.origin`.
  • The "Copy & Close" button did not close the dialog because the clipboard API fails silently in insecure (non-HTTPS) contexts.
  • Both bugs were fixed as part of the Data Explorer session (d75).
  ```

- **Synopsis** (our "narrative"): Flowing prose paragraph giving full context. ~200-400 tokens. Example:
  ```
  Two admin UI bugs were fixed during the d75 session. The invite dialog was constructing invite URLs using hardcoded localhost rather than the current origin, making invites non-functional in non-local deployments. The "Copy & Close" button failed silently because the Clipboard API is unavailable in insecure HTTP contexts — the close action was gated on clipboard success, so it never fired.
  ```

### Data model changes

Add two new fields to the `entities` table:

```sql
ALTER TABLE entities ADD COLUMN key_points TEXT;
ALTER TABLE entities ADD COLUMN synopsis TEXT;
```

- `key_points`: bullet-point summary (stored as plain text with `• ` prefix per point)
- `synopsis`: narrative paragraph
- Existing `content` field remains as the raw/default fallback

### Migration
- Add columns via `Database.ensure_schema()` (the existing pattern for migrations)
- Backfill: entities with `content` but no `key_points`/`synopsis` should show `content` as fallback in both views

### Extraction changes

Update `EXTRACTION_PROMPT` in `src/llm_mem/core/prompts.py` to ask the LLM to produce all three:

```json
{
  "decisions": [{
    "title": "...",
    "content": "...",
    "key_points": ["point 1", "point 2"],
    "synopsis": "Flowing narrative paragraph..."
  }],
  ...
}
```

Update `EntityExtractor._process_job()` in `src/llm_mem/core/extraction.py`:
- Parse `key_points` (list of strings → join with `\n• ` prefix) and `synopsis` from LLM output
- Store in the new entity fields
- If LLM doesn't produce them (backward compat), fall back to `content`

Update `Entity` model in `src/llm_mem/models/entities.py`:
- Add `key_points: str | None = None`
- Add `synopsis: str | None = None`
- Update `to_row()` and `from_row()`

### UI toggle

In `feed_partial.html`, when a card is expanded:

```html
<div class="feed-card-content">
  <div class="feed-card-toggle">
    <button class="toggle-btn toggle-active" data-view="keypoints">Key Points</button>
    <button class="toggle-btn" data-view="synopsis">Synopsis</button>
  </div>
  <div class="feed-card-keypoints">
    {{ item.key_points_html | safe }}
  </div>
  <div class="feed-card-synopsis" style="display:none">
    {{ item.synopsis or item.content }}
  </div>
</div>
```

Toggle buttons switch visibility with JS. Active button gets `toggle-active` class (highlighted, like claude-mem's blue highlight).

### CSS for toggle buttons
- Pill-shaped, side-by-side, similar to claude-mem's `☑ facts` / `📄 narrative`
- Active button: filled background (e.g., blue #3b82f6)
- Inactive: outline/transparent
- Use icons: `☑` for Key Points, `📄` for Synopsis (or simple text)

### Files to modify
- `src/llm_mem/models/entities.py` — add fields
- `src/llm_mem/core/extraction.py` — parse new fields
- `src/llm_mem/core/prompts.py` — update extraction prompt
- `src/llm_mem/core/database.py` — schema migration (add columns)
- `src/llm_mem/ui/templates/feed_partial.html` — toggle UI
- `src/llm_mem/ui/templates/base.html` — toggle CSS + JS
- `src/llm_mem/ui/routes/feed.py` — pass key_points/synopsis to template

---

## 3. More Meaningful Default Summaries

### Problem
Session summaries currently show the raw user prompt text (e.g., "Compare positions/trades/holdings pages (@explore subagent)"). This is not a useful summary for the feed or briefing.

### Required behaviour
The session summary should read like claude-mem's SESSION SUMMARY cards — a structured, human-readable summary. Example:

```
Add export buttons (clipboard/PDF) to Data Explorer chat panel, scoped to output only

🔍 INVESTIGATED
- EllmaPanel.tsx already had ExportBar wired up at line 894

✅ COMPLETED
- Added ExportBar component to ExplorerChat
- Shipped to dev branch

📋 NEXT STEPS
- Test clipboard in HTTPS context
```

### Changes
- Update `SESSION_SUMMARY_PROMPT` in `src/llm_mem/core/prompts.py` to produce a structured summary with sections: title line, INVESTIGATED, LEARNED, COMPLETED, NEXT STEPS
- The summary title should be a clear 1-line description of what the session accomplished, NOT the user's raw prompt
- Use emoji section headers for visual scanning: 🔍 INVESTIGATED, 💡 LEARNED, ✅ COMPLETED, 📋 NEXT STEPS
- Omit empty sections (don't write "Nothing investigated yet")

### Files to modify
- `src/llm_mem/core/prompts.py` — update SESSION_SUMMARY_PROMPT

---

## 4. Fix Event Text Truncation

### Problem
On the session detail page (`/sessions/<id>`), event content is truncated mid-sentence in the table. The Content column cuts off without even an ellipsis.

### Required behaviour
- Show full event content, not truncated
- If content is very long (>500 chars), show first 500 chars with a "Show more" link that expands to full text
- Use `<details><summary>` or JS toggle
- Content should preserve line breaks (use `white-space: pre-wrap` or convert newlines to `<br>`)

### Files to modify
- `src/llm_mem/ui/templates/session_detail.html` — remove any truncation, add show-more pattern
- `src/llm_mem/ui/templates/base.html` — CSS for pre-wrap content in event tables

---

## 5. Better Entity Categories

### Current categories
`decision`, `todo`, `fact`, `failure`, `discovery`

### Required categories
Add coding-specific categories that map to developer mental models:

| Category | Badge colour | When to use |
|----------|-------------|-------------|
| `feature` | green #22c55e | New functionality added |
| `bugfix` | orange #f97316 | Bug identified and/or fixed |
| `research` | purple #8b5cf6 | Investigation, analysis, exploration |
| `decision` | blue #3b82f6 | Architectural/design choice (keep) |
| `todo` | amber #f59e0b | Task to be done (keep) |
| `fact` | cyan #06b6d4 | Durable knowledge (keep) |
| `failure` | red #ef4444 | Error/failure encountered (keep) |
| `discovery` | violet #7c3aed | Notable insight (keep) |
| `change` | slate #64748b | General file/code change |

### Files to modify

1. `src/llm_mem/models/entities.py`
   - Update `EntityType` Literal to include: `"decision", "todo", "fact", "failure", "discovery", "feature", "bugfix", "research", "change"`

2. `src/llm_mem/core/prompts.py`
   - Update `EXTRACTION_PROMPT` to include the new categories with guidance on when to use each

3. `src/llm_mem/core/extraction.py`
   - Update `ENTITY_TYPE_MAP` to include new types

4. `src/llm_mem/ui/templates/base.html`
   - Add CSS badge colours for the new types:
     ```css
     .badge-feature  { background: #22c55e; }
     .badge-bugfix   { background: #f97316; }
     .badge-research { background: #8b5cf6; }
     .badge-change   { background: #64748b; }
     ```

5. `src/llm_mem/ui/templates/feed_partial.html`
   - No changes needed — already uses `badge-{{ item.category }}` dynamically

---

## Acceptance Criteria

1. [ ] Feed cards are collapsed by default (badges + title only)
2. [ ] Clicking a card expands it to show content
3. [ ] Expanded cards have "Key Points" and "Synopsis" toggle buttons
4. [ ] Key Points shows bullet-point summary; Synopsis shows prose paragraph
5. [ ] Toggle state is preserved during htmx 3s polling refresh
6. [ ] `entities` table has `key_points` and `synopsis` columns
7. [ ] Extraction prompt produces key_points + synopsis for each entity
8. [ ] Entities without key_points/synopsis fall back to `content` field
9. [ ] Session summaries have structured format (title + INVESTIGATED/LEARNED/COMPLETED/NEXT STEPS)
10. [ ] Session summaries are human-readable, not raw prompt text
11. [ ] Event content on session detail page shows full text (not truncated)
12. [ ] Long events have "Show more" expand pattern
13. [ ] Entity types include: feature, bugfix, research, change (in addition to existing)
14. [ ] Badge colours are correct for all entity types
15. [ ] All existing tests pass (update as needed for new types/fields)
16. [ ] New tests for: key_points/synopsis extraction, expand/collapse UI, new entity types
17. [ ] `make lint` clean, `make test` all pass
18. [ ] Committed and pushed
