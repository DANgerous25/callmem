# WO-40 — Date Range Filtering and Column-Specific FTS

## Goal

Add date range filtering (`--since` / `--until`) and column-specific FTS queries (`title:auth`) to search, giving the agent and user more precise control over what's returned.

## Background

claude-mem supports both features. llm-mem's FTS5 search currently matches against all indexed columns and has no date filtering. This means a search for recent decisions pulls everything back to project inception, and a search for "auth" in titles also matches content body mentions.

## Deliverables

### 1. Date range filtering

#### MCP `mem_search`

Add optional parameters:

```python
since: str | None = None   # ISO date or relative: "2026-04-01", "7d", "2w", "1m"
until: str | None = None   # Same format
```

Parse relative dates: `7d` = 7 days ago, `2w` = 2 weeks ago, `1m` = 1 month ago.

SQL: add `WHERE created_at >= ? AND created_at <= ?` to the search query.

#### CLI

```bash
llm-mem search "auth" --since 7d -p .
llm-mem search "auth" --since 2026-04-01 --until 2026-04-15 -p .
```

#### Web UI

Add date range picker (two date inputs or preset buttons: "Last 7d", "Last 30d", "This month") to the search toolbar.

### 2. Column-specific FTS

#### Syntax

Support `column:term` syntax in search queries:

- `title:auth` — match "auth" only in entity titles
- `content:migration` — match only in content body
- `type:decision` — match entity type (shortcut for type filter)

Parse the query string to extract column prefixes before passing to FTS5. FTS5 natively supports column filters with the syntax `{column}: term`, so this maps directly.

The FTS5 index must include column names. Check current index definition — if it uses unnamed columns, update to named:

```sql
CREATE VIRTUAL TABLE entities_fts USING fts5(
    title, content, key_points, synopsis,
    content=entities, content_rowid=rowid
);
```

#### MCP `mem_search`

The `query` parameter already accepts a string. Column-specific syntax works within the query string — no new parameters needed. Document it in the tool description.

#### CLI

```bash
llm-mem search "title:auth" -p .
llm-mem search "title:auth content:jwt" -p .
```

### 3. Combined usage

Both features compose:

```bash
llm-mem search "title:auth" --since 7d -p .
```

MCP:
```json
{"query": "title:auth", "since": "7d"}
```

## Constraints

- Python 3.10 compatible
- No AI attribution
- Relative date parsing should be simple — no external dependency (just regex + timedelta)
- If FTS5 index needs updating, write a migration that rebuilds the index (data is preserved)
- Column-specific syntax is optional — plain queries still work as before

## Acceptance criteria

- [ ] `--since` and `--until` work in CLI and MCP search
- [ ] Relative date strings (7d, 2w, 1m) parsed correctly
- [ ] `title:term` column syntax works in FTS5 search
- [ ] Web UI has date range controls
- [ ] Combined column + date filtering works
- [ ] Plain queries still work (no regression)
- [ ] All existing tests pass
