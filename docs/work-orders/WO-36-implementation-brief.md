# WO-36 Implementation Brief

This document gives a cold-start Claude Code agent everything it needs to implement WO-36.

## Repository

```
https://github.com/DANgerous25/llm-mem
```

Python project. `src/llm_mem/` layout. Tests in `tests/`. Run tests with `pytest`. Lint with `ruff check .`.

## What to read first

1. `AGENTS.md` — project norms and conventions
2. `docs/work-orders/WO-36-claude-code-mcp.md` — the full spec
3. `src/llm_mem/cli.py` — the main file you'll modify (setup wizard + init command)
4. `src/llm_mem/core/config.py` — config template generation
5. `tests/` — existing test patterns to follow

## What this work order does

llm-mem is a persistent memory system for coding agents. It currently only integrates with OpenCode (via `opencode.json` MCP config). This WO adds Claude Code support (via `.mcp.json`), so both tools can share the same memory database.

## Key functions to understand

In `cli.py`:

- `_detect_mcp_command(project)` — figures out the right Python path to run the MCP server. Returns a list like `["python3", "-m", "llm_mem.mcp.server", "--project", "."]`. **Reuse this** for Claude Code.
- `_ensure_opencode_instructions(project)` — creates/updates `opencode.json` with MCP config. **Your new function follows the same pattern** but for `.mcp.json`.
- `_ensure_agents_mcp_block(agents_path)` — patches AGENTS.md with MCP tool usage instructions. **Also apply to CLAUDE.md** if it's a separate file.
- `_ensure_opencode_plugin(project)` — installs OpenCode-specific plugins. **Not needed for Claude Code**.
- The `init` command (around line 240) — calls the above functions. Add your new function here.
- The `setup` command (the interactive wizard) — has sections for each config area. Add a "Coding tool integration" section.

## The `.mcp.json` format (Claude Code)

```json
{
  "mcpServers": {
    "llm-mem": {
      "command": "python3",
      "args": ["-m", "llm_mem.mcp.server", "--project", "."]
    }
  }
}
```

Note the difference from OpenCode's format:
- OpenCode: `"command": ["python3", "-m", ...]` (command is the full array)
- Claude Code: `"command": "python3"`, `"args": ["-m", ...]` (split)

## What to implement

### 1. `_ensure_claude_code_mcp(project: Path)`

- Read `.mcp.json` if it exists, else start with `{}`
- Check if `mcpServers.llm-mem` already exists with correct command — if so, no-op
- Split `_detect_mcp_command()` result into command (first element) and args (rest)
- Write/update the `llm-mem` entry under `mcpServers`
- Preserve all other entries in the file
- Print status message

### 2. Tool selection in setup wizard

After the Web UI section, before Autostart:

```
── Coding tool integration ──

  Which coding tools do you use?
    1) OpenCode
    2) Claude Code
    3) Both (default)
    4) Skip
  Choice [default: 3]:
```

Auto-detect: if `opencode.json` exists, pre-select OpenCode. If `.mcp.json` exists, pre-select Claude Code.

### 3. Call from `init` and `setup`

- `init`: call `_ensure_claude_code_mcp(project)` unconditionally (alongside existing `_ensure_opencode_instructions`)
- `setup`: call based on user's tool selection

### 4. CLAUDE.md patching

If `CLAUDE.md` exists and is NOT a symlink to `AGENTS.md`:
- Call `_ensure_agents_mcp_block()` on it too
If it IS a symlink — patching `AGENTS.md` covers it.

### 5. Ollama endpoint tip

In the setup wizard where it prompts for the Ollama endpoint, add a hint:

```
  TIP: If using a VPN that blocks localhost, use your LAN IP
       (e.g. http://192.168.1.100:11434)
```

## Constraints

- Python 3.10 compatible (`from __future__ import annotations` at top)
- No AI attribution in code or comments
- All existing tests must pass
- Write new tests for the Claude Code MCP functions
- `ruff check .` must be clean
- Commit with conventional format: `feat: add Claude Code MCP integration (WO-36)`

## Testing

Run: `pytest` and `ruff check .`

Write tests in a new file `tests/test_claude_code_mcp.py` or add to the existing MCP test file. Test:
- `.mcp.json` created when none exists
- Existing `.mcp.json` with other servers preserved
- Duplicate run is idempotent
- CLAUDE.md symlink detection
