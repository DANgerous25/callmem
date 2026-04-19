# Last Session Summary

**Date:** 2026-04-19 (dfn — work may continue later)
**Duration:** Long session
**Tests:** 557 passing (+75 over yesterday)
**Branch:** `main`, all commits pushed

## What happened

Three main threads, all landed cleanly: Claude Code ingestion
(WO-48, was internal WO-37 before upstream renumber), staleness
detection (real WO-37), and a handful of fixes surfaced along the
way.

### WO-48 — Claude Code session ingestion (commit `ac0b49b`)

Prior sessions had already done WO-36 (MCP *consumer* wiring for
Claude Code). This session added the *ingestion* side, so CC
conversations actually reach the feed. The OpenCode flow is
untouched.

- `adapters/claude_code_import.py` — batch importer. Reads
  `~/.claude/projects/<slug>/*.jsonl`, maps `user` → `prompt`,
  `assistant`/text → `response`, `assistant`/tool_use → `tool_call`.
  Skips `permission-mode`, `attachment`, `system/*`. Idempotent via
  `sessions.metadata.source_id`, dry-run supported, shared
  `.llm-mem/import.lock` pattern.
- `adapters/claude_code.py` — live tailer. Polls the same dir, keeps
  per-file byte offsets in `.llm-mem/claude_code_offsets.json`,
  defers partial trailing lines, resets on shrink, closes sessions
  after `idle_timeout` (default 5 min) or adapter stop.
- Daemon now spawns both adapters side-by-side, each gated by
  `[adapters].opencode` / `[adapters].claude_code` (default on).
- `llm-mem import --source claude-code` CLI.
- 23 importer tests + 8 tailer tests.

### WO-37 (upstream) — Entity staleness detection (commit `9cedc3c`, UI `992d62d`)

Full pipeline so outdated entities stop flooding briefings and
search:

- Migration 007: `entities.stale`, `superseded_by`,
  `staleness_reason` + an index for the default non-stale read.
- `core/staleness.py` `StalenessChecker` — scans entities created
  in the last `lookback_minutes`, FTS-narrows up to 5 older siblings
  of the same type, asks the local LLM for a strict verdict
  (`superseded`/`contradicted`/`coexists`), marks stale on the first
  two. Skips transient types (`change`, `research`).
- Worker wiring — every successful `extract_entities` job enqueues a
  `staleness_check` afterwards.
- Read-path filtering: `engine.search`, `engine.get_entities`,
  `retrieval.search`, `repo.get_entities` all accept `include_stale`
  (default False). Briefing excludes stale and appends
  `(N stale entities suppressed …)` footer.
- Manual controls: `engine.mark_stale/mark_current/list_stale`,
  MCP `mem_mark_stale` + `mem_mark_current` + `include_stale` on
  `mem_search`, CLI `llm-mem stale [--check|--reset <id>]`.
- UI: dimmed card + line-through title + red badge for stale,
  Mark-stale / Mark-current htmx buttons, "Show stale" toggle in
  the feed header, badges/actions on the entity browser table.
- 21 staleness tests total (schema, manual, read filters, MCP,
  automatic with a stub LLM, briefing footer, UI endpoints, CLI).

Audited the auto-detector's output on live data — 3 of 3 "stale"
marks were sensible (2 correct, 1 borderline). No tuning needed.

### Fixes dragged in along the way

- `scripts/setup.py` import paths: the earlier "scoping" fix only
  patched `discover_sessions()`. `import_sessions()` re-ran discover
  internally without the filter and the background subprocess
  dropped `--project-path` entirely. Both paths now forward
  `--project-path`, plus regression tests that monkeypatch
  `subprocess.Popen` (commit `b480e0d`).
- `llm-mem import --status` required `--source` because of Click
  validation order. Dropped `required=True` and raised a
  `UsageError` manually after the `show_status` early-return so the
  error message for the real missing case is unchanged (commit
  `b9c5ee4`).
- Setup port reclaim burned through ports. `_stop_own_service` now
  takes a port and polls `port_available` until the socket truly
  releases instead of a blind 1-second sleep. `port_available`
  sets `SO_REUSEADDR` so stale TIME_WAIT sockets don't false-
  positive (commit `1865104`).
- `retrieval._recency_factor` now treats naive SQLite timestamps as
  UTC so `mark_stale` (which uses `datetime('now')`) doesn't break
  search.
- `staleness._fts_query_from` quotes tokens so hyphenated words
  like `cookie-backed` don't turn into FTS5 `cookie MINUS backed`.

### Cleanup executed mid-session

The memory DBs for `llm-mem`, `screen-lizard`, and `ellma-trading-bot`
were all contaminated with other projects' sessions (bug above).
Wiped and re-imported, scoped correctly — 24/4/26 sessions
respectively. Ports got sorted (back to 9097/9098/9099 after the
reclaim fix). A one-shot script drove the sequence while the
imports ran; details are in the commits.

## Current state

- 557 tests pass.
- Ruff clean in all new code (pre-existing errors in `cli.py:1152`
  and `scripts/setup.py` flagged in TODO.md).
- 11 commits ahead of origin at the time of writing; push is the
  next action on dfn.
- Three project daemons running via systemd user services on
  9097 (llm-mem), 9098 (screen-lizard), 9099 (ellma). All on the
  new code via the editable install, so the CC tailer and
  staleness worker are live.
- `~/.claude/projects/-home-dan-llm-mem/*.jsonl` is already being
  tailed by the daemon — this session's events should appear in
  the feed at http://localhost:9097 as CC keeps writing.

## Key files touched

```
src/llm_mem/adapters/claude_code.py         # new — live tailer
src/llm_mem/adapters/claude_code_import.py  # new — batch importer
src/llm_mem/core/staleness.py               # new — detector
src/llm_mem/core/migrations/007_staleness.sql  # new — schema v7
src/llm_mem/core/engine.py                  # mark_stale, include_stale, CC search
src/llm_mem/core/retrieval.py               # include_stale, timezone fix
src/llm_mem/core/repository.py              # mark_stale, list_stale, get_entity
src/llm_mem/core/briefing.py                # suppress stale + footer count
src/llm_mem/core/workers.py                 # staleness_check handler + auto-enqueue
src/llm_mem/mcp/tools.py                    # mem_mark_stale / mem_mark_current
src/llm_mem/cli.py                          # llm-mem stale, llm-mem import --source
src/llm_mem/models/config.py                # [adapters] section
src/llm_mem/models/entities.py              # stale/superseded_by/staleness_reason
src/llm_mem/ui/routes/entities.py           # POST /stale /current
src/llm_mem/ui/routes/feed.py               # include_stale query, feed extras
src/llm_mem/ui/templates/{base,feed,feed_partial,entities}.html  # badges, toggle, buttons
scripts/setup.py                            # port reclaim + --project-path fixes
README.md                                   # Claude Code section, architecture, config
docs/work-orders/WO-48-claude-code-ingestion.md  # renamed from WO-37 locally
tests/unit/test_{claude_code_import,claude_code_tailer,staleness,setup_port_release,setup_import_scoping}.py
```

## Next steps

- Push `main` (11 commits ahead).
- Decide on WO-38 (concept tags) or WO-40 (date-range column on
  FTS) next — both are smaller than WO-37 and unblock nicer
  filtering.
- Maybe a visual "N stale entities" pill in the feed header so
  users don't have to toggle "Show stale" to discover that some
  were hidden.
- Pre-existing lint cleanup in `cli.py` and `scripts/setup.py` is
  still queued in TODO.md.
