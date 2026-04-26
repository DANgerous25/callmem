# Changelog

All notable changes to callmem are documented here.

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
