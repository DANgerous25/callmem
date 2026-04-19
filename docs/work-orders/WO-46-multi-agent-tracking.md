# WO-46 — Multi-Agent Tracking

## Goal

Track which coding agent (OpenCode/GLM, Claude Code, Kilocode, etc.) produced each session and entity, enabling filtering by agent and cross-agent context handoff.

## Background

The `sessions` table already has an `agent_name` field, but it's not consistently populated and the UI doesn't expose it. With WO-36 adding Claude Code support, users will switch between agents on the same project. Knowing which agent produced which observations is valuable for:

- Filtering the feed by agent
- Understanding which agent made a decision
- Handing off context: "Claude Code worked on auth in session 12, OpenCode continued in session 13"
- Comparing agent productivity/quality

## Deliverables

### 1. Agent detection

In the MCP server, detect which agent is calling:

- **OpenCode/GLM**: User-Agent header or MCP client metadata (check what OpenCode sends)
- **Claude Code**: MCP client metadata or presence of `.mcp.json` config
- **Kilocode**: User-Agent or client identification
- **Fallback**: If detection fails, use "unknown"

Store in `sessions.agent_name` when `mem_session_start` is called. Also add to the `mem_ingest` tool as an optional parameter so the agent can self-identify.

### 2. Entity attribution

Add to entities table:

```sql
ALTER TABLE entities ADD COLUMN agent_name TEXT;
```

Populated from the session's `agent_name` during extraction. Migration for existing entities: set to "opencode" (since all existing data came from OpenCode).

### 3. Web UI

- **Agent badge**: Show small agent icon/badge on each entity card (e.g., "CC" for Claude Code, "OC" for OpenCode)
- **Agent filter**: Add agent filter dropdown to the feed toolbar
- **Session list**: Show agent name in the session list/timeline
- **Agent colours**: Assign a consistent colour per agent (e.g., orange for Claude Code, blue for OpenCode)

### 4. MCP search

Add optional `agent` parameter to `mem_search`:

```python
agent: str | None = None  # filter by agent name
```

### 5. Briefing

In the briefing output, note which agent was used for each session:

```
Session 12 (Claude Code, Apr 18): Implemented JWT auth middleware
Session 13 (OpenCode/GLM, Apr 18): Added rate limiting to auth
```

### 6. CLI

```bash
callmem sessions -p .                    # list sessions with agent column
callmem search "auth" --agent claude -p . # filter by agent
```

## Constraints

- Python 3.10 compatible
- No AI attribution
- Agent detection should be best-effort — never fail or block if detection fails
- Existing entities with no agent_name are valid (display as "—" or "unknown")
- Agent names should be normalized: "claude_code", "opencode", "kilocode", "unknown"

## Acceptance criteria

- [ ] Sessions record which agent started them
- [ ] Entities have agent attribution
- [ ] Web UI shows agent badges on cards
- [ ] Agent filter works in web UI and MCP search
- [ ] Briefing includes agent attribution per session
- [ ] CLI shows agent in session list
- [ ] Migration sets existing data to "opencode"
- [ ] Unknown agents handled gracefully
- [ ] All existing tests pass
