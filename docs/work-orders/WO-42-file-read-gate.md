# WO-42 — File Read Gate

## Goal

Intercept file read tool calls and optionally return the llm-mem observation timeline for that file instead of (or alongside) the raw file content. This is claude-mem's highest-impact feature — it saves ~95% of tokens on re-reads of files the agent has already seen.

## Background

When a coding agent reads a file it has already worked on, it gets the full raw content again — even though it already "knows" the file from prior sessions. claude-mem intercepts the `Read` tool call, checks if the file has observations, and if so returns a compressed timeline: "This file was created in session 3 for JWT auth, modified in session 5 to add refresh tokens, refactored in session 7 to use sessions instead."

This is dramatically cheaper than re-reading the entire file. For a 500-line file, that's ~2000 tokens reduced to ~100 tokens.

## Feasibility

This requires the MCP server to intercept or augment tool calls. Two approaches:

### Approach A: MCP resource (passive)

Register the file timeline as an MCP resource. The agent can choose to check it before reading files. Less invasive but relies on the agent being told to check.

```python
@server.resource("file-context/{path}")
async def file_context(path: str) -> str:
    """Return the observation timeline for a file."""
```

The AGENTS.md instructions tell the agent: "Before reading a file you've worked on before, call `mem_file_context` to get the observation timeline. If it's sufficient, skip the raw read."

### Approach B: Tool wrapper (active)

Provide a `mem_read_file` tool that:
1. Checks if the file has observations in llm-mem
2. If yes: returns the observation timeline + a flag: `has_timeline: true, last_read: 2h ago`
3. The agent decides whether the timeline is sufficient or it needs the raw content
4. If the agent wants raw content, it calls the normal file read tool

### Recommended: Approach B

Approach B gives the agent the information and lets it decide. It's a new MCP tool, not an interception, so it works with any agent that supports MCP tools.

## Deliverables

### 1. New MCP tool: `mem_file_context`

```python
@server.tool("mem_file_context")
async def file_context(
    path: str,                    # relative or absolute file path
    include_content: bool = False # if True, also return current file content
) -> dict:
    """Get the observation timeline for a file.
    
    Returns what llm-mem knows about this file from past sessions:
    changes, decisions, bugs, and current state — often sufficient
    without re-reading the full file.
    """
```

Response format:

```json
{
  "path": "src/auth/middleware.py",
  "has_observations": true,
  "observation_count": 7,
  "first_seen": "2026-04-01",
  "last_modified": "2026-04-18",
  "timeline": [
    {"date": "2026-04-01", "type": "feature", "summary": "Created JWT auth middleware"},
    {"date": "2026-04-05", "type": "bugfix", "summary": "Fixed token expiry check off-by-one"},
    {"date": "2026-04-12", "type": "refactor", "summary": "Switched from JWT to session-based auth"},
    {"date": "2026-04-18", "type": "decision", "summary": "Added rate limiting per-session"}
  ],
  "current_state": "Session-based auth middleware with rate limiting. 47 lines.",
  "recommendation": "Timeline covers recent changes. Raw read recommended only if you need exact implementation details."
}
```

### 2. Query: file observation lookup

Using the existing `entity_files` table (WO-16):
- Join `entity_files` -> `entities` where `entity_files.file_path` matches the requested path
- Order by `entities.created_at`
- Compress into timeline format

Support both exact path match and fuzzy match (strip leading `./`, match basename if full path fails).

### 3. AGENTS.md instruction

Add to the MCP tool instructions block:

```markdown
**Before re-reading a file you've worked on before:**
- Call `mem_file_context` with the file path
- If the timeline is sufficient for your task, skip the raw read (saves tokens)
- If you need exact line-level details, read the file normally
```

### 4. Web UI: file timeline view

On the web UI, add a "Files" tab or section:
- List all files with observations, sorted by last modified
- Click a file to see its observation timeline
- Show observation count per file

### 5. Token savings tracking

Track and display:
- `file_context_calls`: how many times the tool was called
- `reads_avoided`: how many times the agent used the timeline instead of raw read
- Estimated tokens saved (rough: avg file size in tokens × reads avoided)

Show in the web UI dashboard or stats output.

## Constraints

- Python 3.10 compatible
- No AI attribution
- Must not break existing file read workflows — this is an optional tool, not a forced interception
- File path matching must be robust: handle relative paths, `./` prefixes, and case sensitivity
- Timeline should be token-budgeted: if a file has 50 observations, compress/summarize rather than listing all

## Acceptance criteria

- [ ] `mem_file_context` MCP tool returns observation timeline for a file
- [ ] Returns `has_observations: false` for unknown files (not an error)
- [ ] File path matching handles relative paths and basename fallback
- [ ] AGENTS.md instructions updated
- [ ] Web UI shows file timeline view
- [ ] Token savings tracked and displayed
- [ ] All existing tests pass
