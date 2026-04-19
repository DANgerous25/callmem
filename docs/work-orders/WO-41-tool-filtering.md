# WO-41 — Tool Filtering (SKIP_TOOLS)

## Goal

Add a configurable list of tool names to skip during event ingestion, reducing noise from high-frequency or low-value tool calls (e.g., `list_files`, `read_file` on the same file repeatedly).

## Background

claude-mem has a `SKIP_TOOLS` configuration that excludes certain tool call events from being stored. This reduces storage, extraction load, and noise in the observation feed. llm-mem currently ingests all events indiscriminately.

## Deliverables

### 1. Config option

Add to `config.toml`:

```toml
[ingestion]
skip_tools = ["list_files", "glob_tool", "bash"]  # tool names to ignore
skip_patterns = ["read_file*/node_modules/*"]       # glob patterns on tool name + args
```

Default: empty lists (ingest everything). The setup wizard can suggest common noise tools.

### 2. Event filtering

In the SSE adapter and MCP `mem_ingest`:
- Before storing a `tool_call` event, check if the tool name matches `skip_tools` or `skip_patterns`
- If matched, silently drop the event (don't store, don't queue for extraction)
- Log at DEBUG level: `Skipped tool call: {tool_name} (matches skip_tools)`

### 3. Setup wizard

Add a prompt in the setup wizard after the LLM backend section:

```
── Event filtering ──

  Skip noisy tool calls? Common tools to skip:
    list_files, glob_tool, read_file (when reading the same file repeatedly)
  
  Skip tools (comma-separated, or press Enter for none) []:
```

### 4. Web UI settings

Add the skip_tools list to the settings panel so it can be edited from the UI.

### 5. Stats

Add to `llm-mem watch` or a new command:

```bash
llm-mem stats -p .
```

Include a line: `Events skipped (tool filter): N`

Track the count in a simple counter in the DB or in-memory.

## Constraints

- Python 3.10 compatible
- No AI attribution
- Filtering happens at ingestion time, not retroactively
- Already-ingested events from skipped tools are not deleted (backwards compatible)
- Pattern matching uses `fnmatch` (stdlib), no external deps

## Acceptance criteria

- [ ] `skip_tools` config option drops matching tool_call events
- [ ] `skip_patterns` supports glob matching on tool name + arguments
- [ ] Setup wizard prompts for skip_tools
- [ ] Settings panel shows skip_tools
- [ ] Skipped events are not stored or queued for extraction
- [ ] Non-tool_call events (prompt, response, etc.) are never skipped
- [ ] All existing tests pass
