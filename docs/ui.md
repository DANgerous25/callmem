# Web UI

## Overview

callmem includes a local web UI for inspecting, searching, and managing memories. The UI is a practical tool for the project owner, not a polished product — it should be fast to build and easy to extend.

## Technology choice

**FastAPI + htmx + Pico CSS**

Why this stack:
- **FastAPI**: Already in the dependency tree for MCP SSE transport; excellent for serving both API and HTML
- **htmx**: Interactive UI without a JavaScript build step; server-rendered HTML with partial page updates
- **Pico CSS**: Classless CSS framework — semantic HTML looks decent without any styling effort
- No Node.js, no npm, no bundler, no frontend build

This choice optimizes for:
- Minimal dependencies (everything is Python)
- Fast iteration (change a Jinja template, refresh)
- Accessibility from any device (iPad over SSH tunnel, desktop browser)
- Easy for a coding agent to modify

## Pages

### Dashboard (`/`)

The landing page. Quick overview of memory state.

| Section | Content |
|---|---|
| Active project | Project name, root path, DB size |
| Current session | Status, event count, duration |
| Stats | Total events, entities, sessions, last compaction |
| Quick actions | Start compaction, generate briefing, new session |

### Sessions (`/sessions`)

Browse and inspect sessions.

| Feature | Description |
|---|---|
| Session list | Sorted by date (newest first), showing: date, duration, event count, summary snippet |
| Session detail | Click to expand: full summary, event timeline, extracted entities |
| Event timeline | Chronological list of events with type badges (prompt, response, tool_call, etc.) |
| Filters | Date range, status (active/ended/abandoned) |

### Memory search (`/search`)

Full-text search across all memory.

| Feature | Description |
|---|---|
| Search bar | Free-text query input |
| Type filters | Checkboxes: decisions, TODOs, facts, failures, discoveries, events, summaries |
| Results | Ranked list with: type badge, title/snippet, source session, date, relevance score |
| Actions | Pin, edit, delete from results |

### Entities (`/entities`)

Browse structured knowledge by type.

**Sub-pages:**

| Page | Route | Content |
|---|---|---|
| TODOs | `/entities/todos` | Kanban-style: Open / Done / Cancelled columns |
| Decisions | `/entities/decisions` | Timeline of decisions with context |
| Facts | `/entities/facts` | List of project facts, pinned items highlighted |
| Failures | `/entities/failures` | Unresolved vs. resolved, with error context |
| Discoveries | `/entities/discoveries` | Notable findings from sessions |

**Common actions on all entity pages:**
- Pin / unpin
- Edit content
- Delete (soft)
- View source event
- View related entities (via memory edges)

### Briefing (`/briefing`)

Preview and customize the startup briefing.

| Feature | Description |
|---|---|
| Preview | Rendered briefing as the agent would see it |
| Token count | Current token usage vs. budget |
| Component toggles | Show/hide: TODOs, decisions, failures, facts, summary |
| Regenerate | Button to regenerate with current settings |
| Focus filter | Text input to generate a focused briefing |

### Compaction (`/compaction`)

Monitor and control memory compaction.

| Feature | Description |
|---|---|
| Compaction log | History of compaction runs: date, events archived, summaries created |
| Current policy | Display active retention thresholds |
| Manual trigger | Button to run compaction now |
| Preview | Show what would be compacted without executing |

### Settings (`/settings`)

Configuration management.

| Section | Controls |
|---|---|
| Project | Name, root path |
| Ollama | Model name, endpoint URL, connection test button |
| Compaction | Age thresholds (1d, 7d, 30d), max DB size |
| Briefing | Token budget, default focus |
| Embeddings | Enable/disable, backend selection, model (v2) |
| Danger zone | Reset memory, export database, import database |

## Wireframe: Session detail

```
┌─────────────────────────────────────────────────────────────┐
│  callmem  │ Dashboard │ Sessions │ Search │ Entities │ ...  │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  Session: 2026-04-15 14:30 — 16:45                         │
│  Agent: opencode  │  Model: claude-sonnet  │  Events: 47   │
│                                                             │
│  ┌─ Summary ──────────────────────────────────────────────┐ │
│  │ Implemented cursor-based pagination on /api/query.     │ │
│  │ Chose cursor over offset for consistency with          │ │
│  │ streaming results. Added 12 tests. Discovered          │ │
│  │ deadlock issue in concurrent write path — unresolved.  │ │
│  └────────────────────────────────────────────────────────┘ │
│                                                             │
│  ┌─ Extracted Entities ───────────────────────────────────┐ │
│  │ 📌 DECISION: Cursor-based pagination over offset       │ │
│  │ ☐  TODO: Fix concurrent write deadlock (high)          │ │
│  │ ☐  TODO: Add pagination to /api/list endpoint (medium) │ │
│  │ ⚠  FAILURE: Deadlock in test_concurrent_writes         │ │
│  │ 💡 DISCOVERY: SQLite WAL mode helps but doesn't fix    │ │
│  └────────────────────────────────────────────────────────┘ │
│                                                             │
│  ┌─ Event Timeline ──────────────────────────────────────┐  │
│  │ 14:30 [PROMPT]    "Let's add pagination to /api..."   │  │
│  │ 14:31 [RESPONSE]  "I'll implement cursor-based..."    │  │
│  │ 14:32 [TOOL]      write_file: src/api/query.py        │  │
│  │ 14:33 [TOOL]      run_tests: 3 passed                 │  │
│  │ ...                                                    │  │
│  │       [Load more]                                      │  │
│  └────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
```

## API endpoints (for htmx)

All UI interactions are server-rendered HTML fragments returned to htmx.

| Method | Route | Returns |
|---|---|---|
| `GET` | `/` | Dashboard page |
| `GET` | `/sessions` | Sessions list page |
| `GET` | `/sessions/{id}` | Session detail page |
| `GET` | `/search?q=...&types=...` | Search results fragment |
| `GET` | `/entities/{type}` | Entity list by type |
| `POST` | `/entities/{id}/pin` | Updated entity row (htmx swap) |
| `PUT` | `/entities/{id}` | Updated entity (edit form submission) |
| `DELETE` | `/entities/{id}` | Empty (htmx removes row) |
| `GET` | `/briefing` | Briefing preview page |
| `POST` | `/briefing/regenerate` | Fresh briefing fragment |
| `GET` | `/compaction` | Compaction log page |
| `POST` | `/compaction/run` | Trigger compaction, return updated log |
| `GET` | `/settings` | Settings page |
| `PUT` | `/settings` | Save settings |

## Implementation notes

- Templates live in `src/callmem/ui/templates/`
- Static assets (Pico CSS, htmx.js) can be vendored or CDN-loaded
- htmx is a single 14KB JS file — vendor it for offline use
- The UI server binds to `127.0.0.1:9090` by default (configurable)
- Authentication: none for v1 (local-only). Consider basic auth if exposed over network.
- The UI reads/writes the same SQLite database as the MCP server and background workers. SQLite WAL mode handles concurrent access.

## Scope boundaries

**In scope for v1:**
- Browse sessions and events
- Search memories
- View/edit/pin/delete entities
- Briefing preview
- Basic settings

**Out of scope for v1:**
- Real-time updates (polling is fine)
- Mobile-optimized layout
- Dark mode (Pico CSS has it built-in, so it may work for free)
- Drag-and-drop reordering
- Keyboard shortcuts
- Memory graph visualization (v3)
