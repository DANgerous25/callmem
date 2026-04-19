# WO-38 — Concept Tags

## Goal

Add semantic concept tags to entities so they can be filtered and browsed by conceptual category — e.g., "gotcha", "pattern", "trade-off" — in addition to the existing entity type system.

## Background

claude-mem assigns up to 7 concept tags per observation: `how-it-works`, `gotcha`, `pattern`, `trade-off`, `problem-solution`, `why-it-exists`, `what-changed`. These provide a complementary axis to entity types (decision, todo, fact, etc.) and make it easy to answer questions like "show me all the gotchas in this project."

## Deliverables

### 1. Schema changes

New table:

```sql
CREATE TABLE entity_tags (
    entity_id TEXT NOT NULL REFERENCES entities(id),
    tag TEXT NOT NULL,
    PRIMARY KEY (entity_id, tag)
);
CREATE INDEX idx_entity_tags_tag ON entity_tags(tag);
```

Predefined tag vocabulary (stored as a Python constant, not in DB):

```python
CONCEPT_TAGS = [
    "how-it-works",     # Explains a mechanism or architecture
    "gotcha",           # Non-obvious trap, footgun, or surprise
    "pattern",          # Reusable approach or convention
    "trade-off",        # Explicit choice with pros/cons
    "problem-solution", # A problem encountered and how it was solved
    "why-it-exists",    # Rationale for a design or dependency
    "what-changed",     # Documents a change from previous state
]
```

### 2. Extraction prompt update

Extend the entity extraction prompt to request concept tags:

```
For each entity, also assign 0-3 concept tags from this list:
- how-it-works, gotcha, pattern, trade-off, problem-solution, why-it-exists, what-changed

Only assign tags that clearly apply. Most entities will have 0-1 tags.
Return as: "tags": ["gotcha", "pattern"]
```

Update the extraction response parser to read and store tags.

### 3. Web UI

- Show tags as small pill badges in the entity card footer (after the type badge)
- Use a muted colour palette distinct from entity type badges
- Add a tag filter dropdown to the feed toolbar (multi-select, OR logic)
- Tags clickable to filter by that tag

### 4. MCP integration

- `mem_search`: add optional `tags` parameter (list of strings, OR logic)
- `mem_get_briefing`: no change (tags are informational, not structural)
- Entity detail responses include `tags` field

### 5. CLI

```bash
callmem search --tag gotcha -p .           # filter by tag
callmem search --tag gotcha --tag pattern -p .  # OR filter
```

### 6. Re-tagging existing entities

Add a management command:

```bash
callmem retag -p .          # re-run tagging on all entities missing tags
callmem retag --all -p .    # re-tag everything (overwrite)
```

This sends each entity's title + content through the LLM with just the tagging prompt (cheaper than full re-extraction).

## Constraints

- Python 3.10 compatible
- No AI attribution
- Tags are optional — entities with no tags are normal
- Max 3 tags per entity (enforced in parser, not DB)
- If LLM backend is `none`, skip tagging (no tags assigned)
- Re-tagging should use the job queue like extraction does

## Acceptance criteria

- [ ] Extraction produces concept tags on new entities
- [ ] Tags shown as pills in web UI entity cards
- [ ] Tag filter works in web UI feed
- [ ] `mem_search` supports `tags` parameter
- [ ] CLI `--tag` filter works
- [ ] `retag` command processes existing entities
- [ ] Migration runs cleanly on existing databases
- [ ] All existing tests pass
