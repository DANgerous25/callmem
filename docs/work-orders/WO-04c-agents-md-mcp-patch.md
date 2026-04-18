# WO-04c — Auto-patch AGENTS.md with llm-mem MCP Tool Instructions

## Goal

When `llm-mem init` or `llm-mem setup` detects an existing `AGENTS.md` in the project root (e.g. one installed by [coding-norms](https://github.com/DANgerous25/coding-norms)), append the llm-mem MCP tool usage block so the coding agent knows to call the memory tools automatically during sessions.

## Background

Currently, `init`/`setup` handles three cases:

1. **No AGENTS.md exists** — writes the full `templates/AGENTS.md.template` (includes MCP tool instructions). Works perfectly.
2. **AGENTS.md exists, no SESSION_SUMMARY.md reference** — `_ensure_agents_session_summary()` appends the startup briefing snippet. Partial fix.
3. **AGENTS.md exists, already references SESSION_SUMMARY.md** — no-op.

The gap: in case 2 and 3, the MCP tool usage instructions (`mem_ingest`, `mem_search`, `mem_session_start`, `mem_session_end`, `mem_get_tasks`) are never added. The agent connects to the MCP server (tools are listed) but never calls them unless the user explicitly asks. This means:

- No events are ingested during conversation
- The web UI stays empty
- `mem_get_briefing` is never called at session start
- The agent has memory tools available but doesn't know to use them

This is the exact scenario when a project uses the `coding-norms` repo for universal norms and then adds llm-mem on top.

## Deliverables

### 1. New function: `_ensure_agents_mcp_block(agents_path: Path)`

Add a function (next to the existing `_ensure_agents_session_summary`) that:

a) Reads the existing `AGENTS.md`
b) Checks if the MCP tool usage block is already present (look for a sentinel like `mem_ingest` or `## Memory (llm-mem)`)
c) If missing, appends the MCP tool usage block (see below)
d) Writes the file back

The block to append:

```markdown

## Memory (llm-mem)

This project uses llm-mem for persistent memory via MCP tools.

**Start of session:**
1. Read `SESSION_SUMMARY.md` (if it exists) for an auto-generated briefing
2. Call `mem_session_start` to register this session
3. Present a brief summary: greet the user, mention recent activity, highlight open TODOs

**During the session:**
- When you make a design decision, call `mem_ingest` with type "decision"
- When you identify a TODO, call `mem_ingest` with type "todo"
- When you discover something notable, call `mem_ingest` with type "discovery"
- When something fails unexpectedly, call `mem_ingest` with type "failure"
- To recall past context, call `mem_search` with keywords
- To see open tasks, call `mem_get_tasks`

**End of session:**
- Call `mem_session_end` to trigger summary generation

**Guidelines:**
- Be specific in memory content (include file paths, function names, error messages)
- Set priority on TODOs: high, medium, or low
- Mark failures as resolved when you fix them
- The system captures raw events automatically — focus on recording decisions and TODOs
```

### 2. Call from init/setup

In the `init` and `setup` commands, after the existing `_ensure_agents_session_summary(agents_path)` call, add:

```python
_ensure_agents_mcp_block(agents_path)
```

This makes the patching idempotent — if the block is already there (from a previous init or from the full template), it's a no-op.

### 3. Retire SESSION_SUMMARY snippet duplication

The MCP block above includes the SESSION_SUMMARY.md instruction. After appending the MCP block, the separate `_ensure_agents_session_summary` snippet becomes redundant. To handle this cleanly:

- `_ensure_agents_mcp_block` should check for the MCP block sentinel first
- If the MCP block is absent but the old SESSION_SUMMARY snippet IS present, still append the MCP block (it adds the tool instructions the snippet is missing)
- The old snippet doesn't need to be removed — having both is harmless, and removing it risks breaking formatting

### 4. Update the template

The `templates/AGENTS.md.template` content should stay as the "full" version (for the no-AGENTS.md case). No changes needed there — it already has all the MCP instructions.

## Constraints

- Python 3.10 compatible
- No AI attribution
- Idempotent — running init/setup multiple times should not duplicate the block
- Must not overwrite or corrupt existing AGENTS.md content — append only
- Sentinel check should be robust: look for `## Memory (llm-mem)` heading OR `mem_ingest` OR `mem_session_start` (any one is sufficient)

## Acceptance criteria

- [ ] `llm-mem init` on a project with a pre-existing AGENTS.md (from coding-norms) appends the MCP tool usage block
- [ ] Running `llm-mem init` again does not duplicate the block
- [ ] `llm-mem init` on a project with no AGENTS.md still writes the full template (existing behaviour preserved)
- [ ] `llm-mem init` on a project whose AGENTS.md already has the MCP block is a no-op for that file
- [ ] The setup wizard also applies the same patching
- [ ] Print message: `Patched AGENTS.md with llm-mem MCP tool instructions` (or `AGENTS.md already has llm-mem instructions`)
- [ ] All existing tests pass

## Suggested tests

- Unit test: AGENTS.md with no llm-mem content gets MCP block appended
- Unit test: AGENTS.md that already has `mem_ingest` reference is not modified
- Unit test: AGENTS.md with old SESSION_SUMMARY snippet but no MCP block gets the block added
- Unit test: no AGENTS.md → full template written (regression test for existing behaviour)
