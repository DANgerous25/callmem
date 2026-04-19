# WO-37 — Staleness Detection

## Goal

Detect and suppress outdated entities from briefings and search results so the agent doesn't receive contradictory context (e.g., "auth uses JWT" from session 3 when session 7 switched to "auth uses sessions").

## Background

As a project evolves, earlier entities become stale. claude-mem handles this by marking observations as superseded. llm-mem currently returns all entities by recency, which can surface outdated decisions or facts that have since been reversed.

## Deliverables

### 1. Schema changes

Add to the `entities` table:

```sql
ALTER TABLE entities ADD COLUMN stale INTEGER DEFAULT 0;
ALTER TABLE entities ADD COLUMN superseded_by TEXT;  -- entity ID that replaces this one
ALTER TABLE entities ADD COLUMN staleness_reason TEXT;  -- e.g. "superseded", "outdated", "contradicted"
```

Migration in `core/db.py` — follow the existing migration pattern.

### 2. Automatic staleness detection

Add a new worker task `staleness_check` that runs after extraction completes for a session:

a) For each new entity, query existing entities of the same type with overlapping keywords (FTS5 match on title + content)
b) If a new entity covers the same topic as an older one (e.g., both about "authentication method"), send both to the LLM with a prompt:
   ```
   Entity A (older): {title} — {content}
   Entity B (newer): {title} — {content}
   
   Does Entity B supersede or contradict Entity A? Reply with:
   - "superseded" if B replaces A
   - "contradicted" if B directly conflicts with A
   - "coexists" if both are still valid
   ```
c) If "superseded" or "contradicted", mark Entity A as `stale=1`, set `superseded_by` to Entity B's ID, and record the reason.

### 3. Briefing filter

In the briefing generator (`core/briefing.py` or equivalent):
- Exclude entities where `stale=1` from the briefing output
- Add a count at the end: `(N stale entities suppressed)`

### 4. Search filter

In MCP `mem_search` and the web UI search:
- Default: exclude stale entities
- Add optional parameter `include_stale=true` to show them
- Stale entities shown with a visual indicator (strikethrough or dimmed badge) in the UI

### 5. Manual staleness controls

Add MCP tools:
- `mem_mark_stale(entity_id, reason)` — manually mark an entity as stale
- `mem_mark_current(entity_id)` — unmark a stale entity (undo false positive)

Add to web UI entity cards:
- "Mark stale" / "Mark current" toggle button

### 6. CLI command

```bash
llm-mem stale -p .              # list stale entities
llm-mem stale --check -p .      # run staleness check now
llm-mem stale --reset ID -p .   # unmark a stale entity
```

## Constraints

- Python 3.10 compatible
- No AI attribution
- LLM-based checking is optional — if backend is `none`, skip automatic detection (manual marking still works)
- Staleness check should be lightweight: only compare new entities against recent entities (last 30 days), not the entire history
- Must not slow down the extraction pipeline — run as a separate post-extraction step

## Acceptance criteria

- [ ] New entities trigger staleness check against existing entities of the same type
- [ ] Stale entities excluded from briefing by default
- [ ] Stale entities excluded from search by default (with opt-in to include)
- [ ] Web UI shows stale indicator on marked entities
- [ ] Manual mark/unmark works via MCP and web UI
- [ ] CLI `stale` command works
- [ ] Migration runs cleanly on existing databases
- [ ] All existing tests pass
