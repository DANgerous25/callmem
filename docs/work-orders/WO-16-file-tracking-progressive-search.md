# WO-16: File-Level Observation Tracking and Progressive Disclosure Search

## Priority: P1

## Objective

Track which files each entity relates to, and implement a 3-layer progressive disclosure search pattern in the MCP tools to minimise token usage when OpenCode queries memory.

---

## Part 1: File-Level Observation Tracking

### Why

claude-mem tracks which files were read/modified for each observation. This enables powerful queries: "What do we know about `src/auth.py`?", "What was the last change to `config.toml`?". callmem currently has no file associations on entities.

### Data Model

Add a new `entity_files` junction table:

```sql
CREATE TABLE IF NOT EXISTS entity_files (
    entity_id TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    file_path TEXT NOT NULL,
    relation TEXT NOT NULL DEFAULT 'related',  -- 'modified', 'read', 'related'
    PRIMARY KEY (entity_id, file_path)
);
CREATE INDEX IF NOT EXISTS idx_entity_files_path ON entity_files(file_path);
```

And add `entity_files` to `Database.ensure_schema()`.

### Extraction Changes

Update `EXTRACTION_PROMPT` in `src/callmem/core/prompts.py` to ask the LLM to identify files:

```json
{
  "decisions": [{
    "title": "...",
    "content": "...",
    "key_points": ["..."],
    "synopsis": "...",
    "files": ["src/auth.py", "config.toml"]
  }]
}
```

Update `EntityExtractor._process_job()` to:
1. Parse `files` list from extraction output
2. Insert rows into `entity_files` for each file path
3. Also inspect event metadata for file paths (tool_call events often include file paths)

### Query Support

Add to `Repository`:

```python
def get_entities_by_file(self, file_path: str, limit: int = 20) -> list[dict]:
    """Find all entities related to a specific file."""

def get_files_for_entity(self, entity_id: str) -> list[dict]:
    """Get all files associated with an entity."""
```

### MCP Tool

Add a `search_by_file` tool to the MCP server:

```python
@mcp.tool()
def search_by_file(file_path: str, limit: int = 10) -> str:
    """Find all memory entries related to a specific file."""
```

### UI Integration

In the feed card footer, show associated files as small tags:

```html
{% if item.files %}
<span class="feed-card-files">
  {% for f in item.files[:3] %}
  <span class="file-tag">{{ f.file_path | basename }}</span>
  {% endfor %}
</span>
{% endif %}
```

### Files to Create/Modify

- `src/callmem/core/database.py` — add `entity_files` table to schema
- `src/callmem/core/extraction.py` — parse files, insert to `entity_files`
- `src/callmem/core/prompts.py` — add files to extraction prompt
- `src/callmem/core/repository.py` — add `get_entities_by_file()`, `get_files_for_entity()`
- `src/callmem/mcp/server.py` — add `search_by_file` tool
- `src/callmem/ui/routes/feed.py` — attach files to feed items
- `src/callmem/ui/templates/feed_partial.html` — show file tags in card footer

---

## Part 2: Progressive Disclosure Search (3-Layer)

### Why

claude-mem's MCP tools follow a 3-layer pattern that saves 75-80% of tokens vs fetching full details upfront:

1. **Layer 1 — Index**: Compact list (~50-100 tokens/result) — scan what exists
2. **Layer 2 — Timeline**: Chronological context (~100-200 tokens/result) — understand causality
3. **Layer 3 — Full details**: Complete entity content (~500-1000 tokens/result) — deep dive

callmem's current `search_memory` MCP tool returns full content for every result, which is expensive.

### MCP Tool Changes

Rename/restructure the MCP tools to support progressive disclosure:

#### Tool 1: `search` (Layer 1 — Index)

Returns a compact table of matching entities. Cheap to scan.

```
Parameters:
  query: str           — FTS5 search query
  type: str | None     — Filter by entity type
  file_path: str | None — Filter by associated file
  limit: int = 20
  offset: int = 0

Returns (compact table):
  #ID     | Type      | Title                                    | Date       | Files
  E01K... | bugfix    | Fixed invite dialog localhost URL         | 2026-04-17 | UserMgmt.tsx
  E01K... | feature   | Data Explorer Phase 1                    | 2026-04-17 | Explorer.tsx, ExplorerChat.tsx
```

Target: ~50-100 tokens per result.

#### Tool 2: `timeline` (Layer 2 — Timeline)

Returns chronological context around a specific entity or time range.

```
Parameters:
  anchor_id: str | None   — Entity ID to center timeline around
  query: str | None       — Search to find anchor automatically
  depth_before: int = 3   — Entities before anchor
  depth_after: int = 3    — Entities after anchor
  project_id: str | None

Returns:
  Chronological list with key_points (not full content):
  
  2026-04-17 12:40 AM
    #E01K... 🔵 discovery — OpenCode stores sessions in SQLite, not JSON
      • DB at ~/.local/share/opencode/opencode.db
      • Sessions, messages, parts tables with JSON data columns
    
    #E01K... 🟢 feature — OpenCode import adapter rewritten for SQLite  [ANCHOR]
      • Reads from SQLite DB instead of scanning for JSON files
      • Supports filtering by project worktree path
    
    #E01K... 🟢 feature — Memory feed route with real-time htmx polling
      • Card-based layout with category badges
```

Target: ~100-200 tokens per result.

#### Tool 3: `get_entities` (Layer 3 — Full Details)

Returns complete entity content for specific IDs. Use only when you need the full picture.

```
Parameters:
  ids: list[str]   — Entity IDs to fetch (batch multiple in one call)

Returns:
  Full entity with synopsis, key_points, all metadata, associated files.
```

Target: ~500-1000 tokens per result.

### Guidance Prompt

Add a system prompt resource (or include in briefing) that teaches OpenCode the 3-layer pattern:

```
Memory Search Pattern:
  1. search("query") → scan the compact index to find relevant entity IDs
  2. timeline(anchor_id="...") → understand what happened around that entity
  3. get_entities(ids=[...]) → fetch full details only for what you need
  
  Start cheap (Layer 1), go deeper only when needed.
```

### Files to Create/Modify

- `src/callmem/mcp/server.py` — restructure tools: `search` (compact), `timeline`, `get_entities` (full)
- `src/callmem/core/repository.py` — add `get_timeline()` method, update `search()` to return compact format
- `src/callmem/core/briefing.py` — include search pattern guidance in briefing

---

## Acceptance Criteria

### File Tracking
1. [ ] `entity_files` table exists with entity_id, file_path, relation columns
2. [ ] Extraction prompt asks LLM to identify related files
3. [ ] Files from extraction output stored in `entity_files`
4. [ ] `search_by_file` MCP tool returns entities related to a file path
5. [ ] Feed cards show associated file names as tags
6. [ ] `get_entities_by_file()` and `get_files_for_entity()` repository methods work

### Progressive Disclosure
7. [ ] `search` MCP tool returns compact index (~50-100 tokens/result)
8. [ ] `timeline` MCP tool returns chronological context with key_points (~100-200 tokens/result)
9. [ ] `get_entities` MCP tool returns full entity details for specific IDs
10. [ ] Search pattern guidance included in briefing/SESSION_SUMMARY.md
11. [ ] Old `search_memory` tool still works as alias (backward compat) or is cleanly replaced

### General
12. [ ] All existing tests pass, new tests for file tracking and progressive search
13. [ ] `make lint` clean, `make test` all pass
14. [ ] Committed and pushed
