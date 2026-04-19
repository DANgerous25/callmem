# WO-12: OpenCode Adapter, Session Import, and AGENTS.md Template

## Objective

Implement the OpenCode adapter that can subscribe to OpenCode's SSE event stream for automatic memory capture, provide a CLI command and setup wizard step for importing existing OpenCode session history, and create AGENTS.md templates for manual integration.

## Files to create

- `src/callmem/adapters/__init__.py`
- `src/callmem/adapters/opencode.py` — OpenCode SSE event listener (live)
- `src/callmem/adapters/opencode_import.py` — OpenCode session history importer (batch)
- `templates/AGENTS.md.template` — AGENTS.md template for projects using callmem
- `templates/opencode.json.template` — OpenCode config template
- `tests/unit/test_opencode_adapter.py`
- `tests/unit/test_opencode_import.py`

## Files to modify

- `src/callmem/cli.py` — Add `callmem adapter opencode` command and `callmem import` command
- `scripts/setup.py` — Add session import step to the interactive setup wizard
- `pyproject.toml` — Add `httpx-sse` dependency (if needed)

## Constraints

- The adapter connects to OpenCode's SSE event stream (`GET /event` on OpenCode's server)
- It translates OpenCode events into callmem event types
- The adapter runs as a long-lived process alongside the coding session
- It must handle OpenCode server restarts gracefully (reconnect)
- The adapter and MCP server can run in the same process or separately

## Event mapping

| OpenCode event | callmem event type | Content |
|---|---|---|
| `message.created` (role=user) | `prompt` | User message text |
| `message.created` (role=assistant) | `response` | Assistant message text |
| `tool.invoked` | `tool_call` | Tool name + args summary |
| `file.changed` | `file_change` | File path + change summary |
| `session.created` | (trigger `session_start`) | — |
| `session.completed` | (trigger `session_end`) | — |

Note: The exact OpenCode SSE event schema may need adjustment based on the actual API. Design the adapter with a mapping layer that's easy to update.

## AGENTS.md template

```markdown
# Project Memory — callmem

This project uses callmem for persistent memory across coding sessions.

## Session workflow

**Start of session:**
1. Call `mem_session_start` to register this session
2. The returned briefing contains important context from previous sessions
3. Read the briefing carefully before starting work

**During the session:**
- When you make an important decision, call `mem_ingest` with type "decision"
- When you identify a TODO, call `mem_ingest` with type "todo"
- When you discover something notable, call `mem_ingest` with type "discovery"
- When something fails unexpectedly, call `mem_ingest` with type "failure"
- To recall past context about a topic, call `mem_search`
- To see open tasks, call `mem_get_tasks`

**End of session:**
- Call `mem_session_end` to trigger summary generation

## Memory guidelines
- Be specific in memory content (include file paths, function names, error messages)
- Set priority on TODOs: high, medium, or low
- Mark failures as resolved when you fix them: `mem_update_task`
- You don't need to memorize everything — the system captures raw events automatically
- Focus on recording decisions (the "why") and TODOs (what's next)
```

## Session import — CLI command

Users adding callmem to an existing project will have OpenCode session history they want to backfill. Provide a CLI command:

```
callmem import --source opencode [--session-id <id>] [--session-dir <path>] [--project <path>] [--all] [--dry-run]
```

- `--source` — Required. Only `opencode` for now, but design for extensibility.
- `--session-id` — Import a specific session by its OpenCode ID.
- `--session-dir` — Override OpenCode session directory (default: `~/.local/share/opencode/`).
- `--project` — Project root (default: `.`).
- `--all` — Import all discovered sessions. Without `--all` or `--session-id`, list what's available.
- `--dry-run` — Show what would be imported without actually importing.

### Import logic

1. Discover JSON session files under the session directory (`rglob("*.json")`)
2. Read each file, filter for those containing a `messages` array
3. For each session:
   a. `engine.start_session(agent_name="opencode")`
   b. Map messages: `role=user` → `prompt`, `role=assistant` → `response`
   c. Map `tool_calls` array → `tool_call` events (name + truncated args)
   d. Map `parts` with `type=file_change` → `file_change` events
   e. Handle both flat `content` strings and structured `parts`/`content` arrays
   f. `engine.ingest()` each batch (dedup, redaction, entity extraction all apply)
   g. `engine.end_session(session.id, note=title)`
4. Print summary: sessions imported, events ingested, any errors

### OpenCode JSON format

Handle flexible structures — flat content strings and structured parts:

```json
{
  "id": "session_id",
  "title": "...",
  "messages": [
    {"role": "user", "content": "..."},
    {"role": "assistant", "content": "...", "parts": [...], "tool_calls": [...]}
  ]
}
```

## Session import — setup wizard step

The setup wizard (`scripts/setup.py`) must offer to import existing sessions during first-time setup. This runs after database creation and before OpenCode MCP configuration.

### Flow

1. Check `~/.local/share/opencode/` for JSON files with messages
2. If none found, skip silently
3. If found, show a preview: up to 10 sessions with truncated ID, title, and message count
4. Ask: `Import these sessions into callmem? [Y/n]`
5. If yes: call `import_sessions(engine, session_dir, import_all=True)`, print summary
6. If no: print the manual CLI command for later use
7. On failure: print error and the manual CLI command (never block setup completion)

The "Next steps" section at the end of setup must also include the import command.

### Re-run safety

The import step must be safe to run multiple times. The engine's dedup window handles exact duplicate events within 60 seconds, but re-importing sessions from earlier runs may create duplicate sessions. This is acceptable — users can delete duplicates from the UI.

## Acceptance criteria

### Live adapter
1. `callmem adapter opencode --opencode-url http://localhost:4096 --project .` connects to OpenCode's SSE stream
2. User messages are captured as `prompt` events
3. Assistant messages are captured as `response` events
4. Tool invocations are captured as `tool_call` events
5. OpenCode session start/end maps to callmem session lifecycle
6. Adapter reconnects if OpenCode server disconnects

### Session import CLI
7. `callmem import --source opencode --dry-run` lists discovered sessions without importing
8. `callmem import --source opencode --all` imports all sessions with correct event mapping
9. `callmem import --source opencode --session-id <id>` imports only the matching session
10. Handles both flat content strings and structured parts/content arrays
11. Tool calls are mapped with name and truncated args (max 200 chars)
12. File changes from message parts are mapped as `file_change` events
13. Each imported session is properly started and ended with the original title as summary
14. Import prints a summary: session count, event count, errors

### Setup wizard integration
15. `make setup` / `callmem setup` shows "Existing session history" section when OpenCode sessions exist
16. Previews up to 10 sessions with ID, title, and message count
17. On "yes", imports all sessions and prints a summary
18. On "no", prints the manual `callmem import` command
19. Skips silently when no OpenCode session directory or no valid sessions found
20. Never blocks setup completion on import failure

### Templates and tests
21. AGENTS.md template is generated by `callmem init` alongside config
22. OpenCode config template works when placed in `opencode.json`
23. `pytest tests/unit/test_opencode_adapter.py tests/unit/test_opencode_import.py` passes

## Suggested tests

```python
# Live adapter — mock SSE stream
def test_adapter_captures_prompt(mock_sse_stream, engine):
    mock_sse_stream.emit({"type": "message.created", "data": {"role": "user", "content": "Fix the bug"}})
    adapter = OpenCodeAdapter(engine, "http://localhost:4096")
    adapter.process_event(mock_sse_stream.last_event)
    events = engine.get_events(type="prompt")
    assert len(events) == 1
    assert "Fix the bug" in events[0].content

def test_adapter_maps_tool_call(mock_sse_stream, engine):
    mock_sse_stream.emit({"type": "tool.invoked", "data": {"tool": "write_file", "args": {"path": "src/api.py"}}})
    adapter = OpenCodeAdapter(engine, "http://localhost:4096")
    adapter.process_event(mock_sse_stream.last_event)
    events = engine.get_events(type="tool_call")
    assert len(events) == 1

# Session import — file-based
def test_import_maps_user_to_prompt(tmp_path, engine):
    session_file = tmp_path / "session.json"
    session_file.write_text(json.dumps({
        "id": "test-1", "title": "Test",
        "messages": [{"role": "user", "content": "hello"}]
    }))
    results = import_sessions(engine, tmp_path, import_all=True)
    assert results[0]["event_count"] == 1

def test_import_handles_structured_content(tmp_path, engine):
    session_file = tmp_path / "session.json"
    session_file.write_text(json.dumps({
        "id": "test-2", "title": "Structured",
        "messages": [{"role": "assistant", "content": [{"text": "part1"}, {"text": "part2"}]}]
    }))
    results = import_sessions(engine, tmp_path, import_all=True)
    assert results[0]["event_count"] >= 1

def test_import_dry_run_no_side_effects(tmp_path, engine):
    session_file = tmp_path / "session.json"
    session_file.write_text(json.dumps({
        "id": "test-3", "title": "Dry",
        "messages": [{"role": "user", "content": "test"}]
    }))
    results = import_sessions(engine, tmp_path, dry_run=True)
    assert results[0]["dry_run"] is True
    assert engine.list_sessions() == []  # nothing imported

def test_import_maps_tool_calls(tmp_path, engine):
    session_file = tmp_path / "session.json"
    session_file.write_text(json.dumps({
        "id": "test-4", "title": "Tools",
        "messages": [{"role": "assistant", "content": "",
            "tool_calls": [{"function": {"name": "write_file", "arguments": "{\"path\": \"src/main.py\"}"}}]
        }]
    }))
    results = import_sessions(engine, tmp_path, import_all=True)
    assert results[0]["event_count"] >= 1
```
