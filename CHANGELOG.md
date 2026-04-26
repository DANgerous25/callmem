# Changelog

All notable changes to callmem are documented here.

## [0.3.0] — 2026-04-26

### Security
- **EventBus thread-safety**: Added `threading.Lock` protecting subscriber list — fixes data race between worker threads (publish) and async SSE handlers (subscribe/unsubscribe)
- **SRI integrity hash**: Added `integrity` attribute to PicoCSS CDN `<link>` tag
- **SECURITY.md**: Added vulnerability reporting policy

### Bug fixes
- **compaction_log column mismatch**: `summaries_archived` now correctly written to `summaries_created` column; entity count preserved in `entities_merged`
- **FTS5 cross-project data leakage**: `engine.search_fts()` now filters by `project_id`; added `project_id` parameter to the method signature

### Repository pattern enforcement
- Added 10 new repository methods: `get_events_by_ids`, `count_all`, `get_session_event_ids_for_summary`, `count_ended_sessions`, `insert_summary`, `search_events_fts`, `search_entities_fts_by_type`, `get_entity_by_short_id`, `list_projects`, `get_sessions_by_ids`
- Refactored `engine.py`: 4 direct DB bypasses eliminated (`search_fts`, `compress_context` insert, `_maybe_queue_chunk_summary`, `_maybe_queue_cross_session_summary`)
- Refactored `retrieval.py`: `_search_fts` uses `repo.search_events_fts`
- Refactored `mcp/tools.py`: `handle_get_entities` uses `repo` methods
- Refactored `ui/routes/dashboard.py` and `feed.py`: all direct SQL replaced with repo calls

### Queue
- **Atomic dequeue**: `JobQueue.dequeue()` now uses a single `UPDATE ... RETURNING *` statement — eliminates race condition when claiming jobs

### Extraction hardening
- `ollama._generate()` calls replaced with `ollama.extract()` across extraction, summarization, and knowledge modules
- Added `MAX_EVENTS_PER_JOB` guard (default 50) — large event batches are split into multiple extraction jobs to avoid exceeding the LLM context window

### Packaging
- Version bumped to 0.3.0
- Added `py.typed` marker for PEP 561 compliance
- Pinned `mcp>=1.0,<2.0` to prevent MCP SDK 2.0 breakage
- Added `Framework :: MCP` and `Environment :: Web Environment` classifiers
- Added `CODE_OF_CONDUCT.md` (Contributor Covenant v2.1)
- Added `config.toml.example` with all settings documented
- Added GitHub Actions CI + publish workflows
- Added issue templates (bug report, feature request)

### Documentation
- Removed internal work-orders (56 files), prompts, and getting-started from `docs/`
- Rewrote README for pip-first workflow
- Added release checklist
- Sanitized CHANGELOG personal repo paths

## [0.2.0] — 2026-04-19

### Renamed from `llm-mem` to `callmem`

The project has a new name. Everything works the same; identifiers changed:

| Thing | Before | After |
|---|---|---|
| PyPI package | `llm-mem` | `callmem` |
| Python module | `llm_mem` | `callmem` |
| CLI binary | `llm-mem` | `callmem` |
| MCP server id | `llm-mem` | `callmem` |
| Project config dir | `.llm-mem/` | `.callmem/` |
| Global config | `~/.config/llm-mem/` | `~/.config/callmem/` |
| Env var prefix | `LLM_MEM_` | `CALLMEM_` |
| Systemd unit | `llm-mem-<proj>.service` | `callmem-<proj>.service` |
| GitHub repo | `llm-mem/llm-mem` | `callmem/callmem` |

### Backward compatibility

Existing installs keep working unchanged. All legacy names are honored as
fallbacks so upgrading a project that's still on `llm-mem` just works:

- `llm-mem` console script still runs (aliased to `callmem`)
- `python -m llm_mem.mcp.server` still works (shim re-exports the new module)
- `.llm-mem/config.toml` is still loaded if `.callmem/` doesn't exist
- `~/.config/llm-mem/config.toml` still loaded if the new global path is missing
- `LLM_MEM_*` env vars are honored (any matching `CALLMEM_*` wins)
- `LLM_MEM_API_KEY` and `LLM_MEM_VAULT_PASSPHRASE` still work as fallbacks
- Setup script's systemd port-conflict scan includes legacy `llm-mem-*.service` units

A `DeprecationWarning` fires when the legacy `llm_mem` package is imported; no
other messages unless configured.

### New command

- **`callmem migrate`** — one-shot, idempotent migration for a project:
  renames `.llm-mem/` → `.callmem/` and rewrites the MCP server key/command
  in `.mcp.json` and `opencode.json`. Run `callmem migrate --dry-run` to
  preview changes first.

## [0.1.0] — 2026-04-17

Initial release.

### Core
- SQLite database with FTS5 full-text search
- Event ingestion pipeline with deduplication and truncation
- Entity extraction via local LLM (Ollama, OpenAI-compatible, or pattern-only)
- Entity types: feature, bugfix, discovery, decision, todo, fact, failure, research, change
- Dual content views: Key Points (bullet-point) and Synopsis (narrative prose)
- Layered compression: chunk summaries, session summaries, cross-session summaries
- Memory compaction with age-based archival
- Sensitive data detection (pattern + LLM) with encrypted vault
- Job queue with background worker dispatch

### Briefing
- Structured startup briefing with context economics
- Emoji-coded observation timeline grouped by date and session
- Automatic `SESSION_SUMMARY.md` generation in project root
- Token budgeting with configurable max_tokens

### Web UI
- Card-based memory feed as primary dashboard
- Expandable cards with Key Points / Synopsis toggle
- Real-time updates via Server-Sent Events (SSE)
- Colour-coded category badges for all entity types
- Session browser with event timeline
- Full-text search page
- TODO tracker
- Briefing preview page
- Stats dashboard
- Dark theme (Pico CSS)

### Integrations
- OpenCode SSE adapter for real-time event capture
- OpenCode session import from SQLite database
- MCP server with search, briefing, and task tools
- Pluggable LLM backend: Ollama, OpenAI-compatible, or none

### CLI
- Interactive setup wizard with Ollama auto-detection and port conflict checking
- `daemon` command (UI + workers + adapter in one process)
- `import` command for OpenCode session history
- systemd user service integration with auto-start
- Multi-project daemon support with port conflict detection

### Developer Experience
- 382+ tests (unit + integration)
- Ruff linting with strict type checking
- Makefile with test, lint, check, daemon, start/stop/restart targets
- Work order system for structured implementation
- Bootstrap memory files (SESSION.md, DECISIONS.md, TODO.md) with quick commands
