# WO-36 — Claude Code MCP Integration

## Goal

Add Claude Code as a supported coding tool alongside OpenCode. The setup wizard should detect and/or ask which tools are in use and configure MCP for each. Both can be enabled simultaneously — the shared SQLite database is the single source of truth, giving both tools access to the same memory.

## Background

llm-mem currently only integrates with OpenCode (`opencode.json` MCP config, `.opencode/` plugins, OpenCode SSE adapter). Claude Code also supports MCP servers via `.mcp.json` in the project root and reads agent instructions from `CLAUDE.md`.

The user switches between Claude Code and OpenCode on the same projects. When one tool hits a rate limit or when different tasks suit different tools, they want to pick up where the other left off — with shared memory context.

### Network consideration

Claude Code requires ExpressVPN (for region access), which conflicts with Tailscale. So when using Claude Code, the user cannot reach services via Tailscale IPs. The Ollama endpoint and llm-mem web UI must be configured using the machine's **local LAN IP** (e.g. `192.168.x.x`) rather than `localhost` or Tailscale IP. The setup wizard should ask for the host/IP to bind to and use for service URLs.

## Deliverables

### 1. Detect and configure Claude Code MCP (`.mcp.json`)

Add a new function `_ensure_claude_code_mcp(project: Path)` alongside the existing `_ensure_opencode_instructions()`:

```python
def _ensure_claude_code_mcp(project: Path) -> None:
    """Ensure .mcp.json has llm-mem MCP server configured for Claude Code."""
```

The `.mcp.json` format:

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

Key differences from OpenCode:
- File is `.mcp.json` (not `opencode.json`)
- Schema uses `"command"` + `"args"` array (not `"command"` as array)
- No `"type": "local"` or `"enabled"` fields
- Must preserve any other MCP servers already in the file (e.g. user may have other tools configured)

Use the same `_detect_mcp_command()` to determine the right Python executable path, but split it into command + args for Claude Code's format.

### 2. Setup wizard: tool selection prompt

In the setup wizard, after the Web UI section and before autostart, add:

```
── Coding tool integration ──

  Which coding tools do you use? (select all that apply)
    1) OpenCode  [detected: opencode.json exists]
    2) Claude Code  [detected: .mcp.json exists]
    3) Both
    4) Neither (MCP tools only, manual integration)
  Choice [default: auto-detected or Both]:
```

Auto-detection:
- If `opencode.json` (or `.opencode.json`, `opencode.jsonc`) exists → OpenCode detected
- If `.mcp.json` exists → Claude Code detected
- If `CLAUDE.md` exists (and is not just a symlink to AGENTS.md) → Claude Code likely
- If neither detected, default to "Both" (safe — configures both, no harm if unused)

Based on selection:
- **OpenCode**: run `_ensure_opencode_instructions()` + `_ensure_opencode_plugin()` (existing)
- **Claude Code**: run `_ensure_claude_code_mcp()` (new)
- **Both**: run all of the above
- **Neither**: skip MCP config, just print manual instructions

### 3. CLAUDE.md handling

The `coding-norms` installer already creates `CLAUDE.md` as a symlink to `AGENTS.md`. The llm-mem setup should:

a) If `CLAUDE.md` exists (symlink or file) → patch it with MCP tool instructions via the existing `_ensure_agents_mcp_block()` (same as AGENTS.md patching from WO-04c)
b) If `CLAUDE.md` is a symlink to `AGENTS.md` → patching AGENTS.md is sufficient (the symlink follows)
c) If `CLAUDE.md` is a separate file → patch it independently (it may have Claude-specific instructions)

### 4. Ollama endpoint: configurable host

In the setup wizard's Ollama configuration section, the endpoint currently defaults to `http://localhost:11434`. Change the prompt to:

```
  Ollama endpoint [http://localhost:11434]:
  TIP: If using a VPN that blocks localhost access from other tools,
       use your LAN IP (e.g. http://192.168.1.100:11434)
```

Store in `config.toml` as-is. This already works — just needs the prompt hint.

### 5. Web UI bind: same treatment

The bind address prompt already exists. Add a similar tip:

```
  Bind address (0.0.0.0 for network, 127.0.0.1 for local only) [0.0.0.0]:
  TIP: 0.0.0.0 makes the UI accessible from your LAN. Use this if you
       switch between VPN and Tailscale.
```

### 6. `init` command: lightweight version

The `init` command (non-interactive) should also configure Claude Code MCP if `.mcp.json` exists or `CLAUDE.md` exists. Add `_ensure_claude_code_mcp(project)` call after the existing `_ensure_opencode_instructions(project)`.

## File changes

| File | Change |
|---|---|
| `src/llm_mem/cli.py` | Add `_ensure_claude_code_mcp()`, add tool selection to setup wizard, call from both `init` and `setup` |
| `src/llm_mem/core/config.py` | No changes needed (endpoint is already user-configurable) |
| `templates/` | Add `claude/.mcp.json.template` if desired (optional — the function can generate it inline) |

## Constraints

- Python 3.10 compatible
- No AI attribution
- Idempotent — re-running setup must not duplicate MCP entries
- Must preserve existing entries in `.mcp.json` (user may have other MCP servers)
- Must preserve existing entries in `opencode.json` (existing behaviour)
- The `_detect_mcp_command()` logic is shared between both tools

## Acceptance criteria

- [ ] `llm-mem setup` asks which coding tools to configure
- [ ] Selecting "Claude Code" creates/updates `.mcp.json` with llm-mem MCP server
- [ ] Selecting "OpenCode" configures `opencode.json` (existing behaviour preserved)
- [ ] Selecting "Both" configures both files
- [ ] `llm-mem init` auto-detects and configures both if config files present
- [ ] Existing `.mcp.json` entries (other MCP servers) are preserved
- [ ] CLAUDE.md gets MCP tool instructions block (if it's a separate file from AGENTS.md)
- [ ] Re-running setup/init does not duplicate entries
- [ ] Ollama endpoint prompt includes LAN IP tip
- [ ] All existing tests pass

## Suggested tests

- Unit test: `.mcp.json` created correctly when none exists
- Unit test: existing `.mcp.json` with other servers preserved, llm-mem added
- Unit test: `.mcp.json` already has llm-mem → no-op
- Unit test: CLAUDE.md symlink detected → only AGENTS.md patched
- Unit test: CLAUDE.md separate file → patched independently
- Integration test: setup wizard with "Both" selected configures both files
