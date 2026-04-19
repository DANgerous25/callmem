# WO-37 — Claude Code Session Ingestion

## Goal

Ingest Claude Code (CC) transcripts into callmem so the feed, briefings, and search reflect work done in CC — just like it already does for OpenCode. Follow-up to WO-36, which only made callmem's MCP tools *available* to CC; it did not feed CC's own conversations back in.

## Background

Today callmem has two ingestion paths, both OpenCode-only:

- `src/callmem/adapters/opencode.py` — live SSE listener on `http://localhost:4096`, translated by `process_event()` into `EventInput` records.
- `src/callmem/adapters/opencode_import.py` — batch importer that reads OpenCode's SQLite DB (`~/.local/share/opencode/opencode.db`) and replays historical sessions.

There is no equivalent for Claude Code. As a result, any project where the user works primarily in CC shows a stale / empty feed. Every row in today's `.callmem/memory.db` across all projects has `agent_name='opencode'`.

Claude Code stores transcripts as JSONL on disk:

```
~/.claude/projects/<slug>/<sessionId>.jsonl
```

where `<slug>` is the project's absolute path with `/` replaced by `-` (e.g. `/home/dan/callmem` → `-home-dan-callmem`). Each line is one event. The file is append-only — CC appends a new line after every user message, assistant response, tool call, permission change, etc.

## Transcript schema (observed)

Per-record keys vary by `type`. Common top-level fields: `type`, `timestamp` (ISO 8601 UTC with `Z`), `sessionId`, `uuid`, `parentUuid`, `cwd`, `gitBranch`, `version`.

Observed `type` distribution in one 306 KB transcript:

| type                      | count | meaning                                                  |
|---------------------------|------:|----------------------------------------------------------|
| `assistant`               |    82 | Assistant message (content blocks inside `message`)      |
| `user`                    |    61 | User message OR tool result (role=`user`, string content)|
| `permission-mode`         |    10 | Permission mode switch (skip)                            |
| `attachment`              |    10 | File attachment (skip for MVP)                           |
| `last-prompt`             |     9 | Bookkeeping (skip)                                       |
| `file-history-snapshot`   |     6 | File backup state (skip)                                 |
| `system/turn_duration`    |     4 | Timing metadata (skip)                                   |
| `system/local_command`    |     2 | `/slash` command execution                               |
| `system/away_summary`     |     1 | User returning after a break — useful session boundary   |
| `system/informational`    |     1 | Skip                                                     |

`user` records: `message.content` is either a string (user prompt) or a list of content blocks (for tool results — identifiable by `tool_use_id`). `assistant` records: `message.content` is a list of blocks, each `type` ∈ `{text, tool_use, thinking}`.

## Deliverables

### 1. `src/callmem/adapters/claude_code_import.py`

Historical-batch importer, parallel to `opencode_import.py`. Public API:

```python
def discover_sessions(
    project_path: Path,
    claude_projects_dir: Path | None = None,  # default: ~/.claude/projects
) -> list[dict[str, Any]]:
    """Return session summaries for a project (filename → sessionId,
    first/last timestamp, message_count, first user prompt as title)."""

def import_session(
    engine: MemoryEngine,
    jsonl_path: Path,
    progress_cb: ProgressCallback | None = None,
) -> dict[str, Any]:
    """Replay one CC transcript into callmem. Idempotent via session_id."""

def import_sessions(
    engine: MemoryEngine,
    project_path: Path,
    since: datetime | None = None,
    progress_cb: ProgressCallback | None = None,
) -> dict[str, Any]:
    """Import all CC sessions for this project. Reuses the same
    .callmem/import.lock and import_progress.json files."""
```

Event mapping (MVP — skip what's in parentheses):

| CC record                                            | callmem EventInput |
|------------------------------------------------------|--------------------|
| `user` + `message.content` is str, not `<command-*>` | `prompt`           |
| `user` + content blocks with `tool_use_id`           | `tool_result`      |
| `assistant` text block                               | `response`         |
| `assistant` tool_use block                           | `tool_call` (content = `{tool}({args})`) |
| `assistant` thinking block                           | (skip for MVP)     |
| `system/local_command`                               | `prompt` (marked as slash command) |
| everything else                                       | skip               |

Derive `agent_name='claude-code'` and `model_name` from `assistant.message.model` when present.

### 2. `src/callmem/adapters/claude_code.py`

Live tailer. Uses `watchdog` (already pulled in? check pyproject; if not, poll-based `os.stat` every 2s is acceptable and keeps the dep surface smaller — OpenCode's adapter already polls on reconnect). Responsibilities:

- Watch `~/.claude/projects/<slug>/` for new or growing `.jsonl` files where `<slug>` is derived from `project_path`.
- Maintain per-file byte offset in `.callmem/claude_code_offsets.json` so restarts resume mid-file.
- On new lines, parse and call the same mapping used by the importer — share code, don't duplicate.
- Session lifecycle: open a session on the first record of a new file; close it on `system/away_summary`, or when the file has been idle for `CLAUDE_SESSION_IDLE_SECONDS` (default 300), or on adapter shutdown.

### 3. Wire into the daemon

In `cli.py` `daemon()` (around line 548):

```python
if not no_adapter:
    from callmem.adapters.opencode import OpenCodeAdapter
    from callmem.adapters.claude_code import ClaudeCodeAdapter
    ...
    cc_adapter = ClaudeCodeAdapter(engine, project_path=project)
    threading.Thread(target=cc_adapter.run, daemon=True,
                     name="callmem-claude-code-adapter").start()
```

Gate each adapter behind a config flag so users can disable either:

```toml
[adapters]
opencode = true    # default true (back-compat)
claude_code = true # default true if ~/.claude/projects/<slug> exists
```

Keep `--no-adapter` as the master off-switch.

### 4. CLI entry point for manual import

```
callmem import --source claude-code --project . [--all | --since <ts>]
```

Mirror the existing `--source opencode` flag's ergonomics.

### 5. Setup wizard

During the "Coding tool integration" block added by WO-36, after detecting CC via `.mcp.json` or `CLAUDE.md`, also offer:

```
Import existing Claude Code session history for this project? [Y/n]
```

If yes, call `claude_code_import.import_sessions()` with a progress callback.

### 6. Tests

New file `tests/unit/test_claude_code_import.py`:

- Fixture jsonl with 3–4 representative records (user prompt, assistant text, assistant tool_use, user tool_result). Keep under 20 lines.
- `test_maps_user_prompt_to_prompt_event`
- `test_maps_assistant_text_to_response_event`
- `test_maps_assistant_tool_use_to_tool_call`
- `test_maps_user_tool_result_to_tool_result`
- `test_skips_permission_mode_and_snapshots`
- `test_slash_commands_are_ingested_as_prompts`
- `test_idempotent_reimport` — running `import_session` twice leaves the DB unchanged

New file `tests/unit/test_claude_code_live.py`:

- `test_tailer_resumes_from_offset` — write half a file, tail, write the rest, assert only the new lines are ingested.
- `test_idle_timeout_ends_session` — monkeypatch `CLAUDE_SESSION_IDLE_SECONDS=0.1`.
- `test_slug_derivation_for_project_path` — `/home/dan/callmem` → `-home-dan-callmem`.

## Non-goals

- Ingesting thinking blocks (noisy; reconsider later).
- Ingesting attachments (`attachment` records) as first-class events — just record a lightweight `note` with the filename if desired.
- Two-way sync (callmem does not write back to CC transcripts, ever).
- Watching every project under `~/.claude/projects/` — only the ones associated with a given callmem daemon's `--project`.

## Open questions

1. **Multi-instance CC sessions.** If the user has two CC windows open on the same project, two jsonl files grow concurrently. The tailer must handle N files at once — not assume a single "current" session.
2. **Sidechain records.** `isSidechain: true` appears on subagent messages. Decision: ingest but tag `metadata.sidechain=true` so the feed can hide them by default.
3. **Timestamps.** CC uses `Z`-suffixed UTC. OpenCode's importer converts to `+00:00` — reuse `callmem.compat.UTC` helpers so timestamps are consistent in the DB.
4. **Schema migration?** Likely unnecessary — `sessions.agent_name` already stores free-form strings. But confirm no enum constraint exists before proceeding.

## Risks

- **Disk pressure.** Large transcripts (100K+ lines) could flood the job queue. Mitigate by batching ingests (`engine.ingest_many()` if it exists; otherwise add it) and by respecting the existing chunk_size config.
- **Re-entrant ingestion.** An active CC session writing to a file while the importer reads it → need to seek to EOF on handoff between importer and live tailer.
- **PII.** CC transcripts contain full user prompts including file contents. Existing `redaction.py` pipeline must run on every ingested event — verify it's in the engine path.

## Constraints

- Python 3.10+ compatible (`from __future__ import annotations` at top of each new file).
- No AI attribution in code comments.
- `ruff check .` must be clean on all new files.
- All existing tests must pass.
- Conventional commit: `feat: add Claude Code session ingestion (WO-37)`.
