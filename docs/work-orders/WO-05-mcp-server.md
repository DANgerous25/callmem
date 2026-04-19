# WO-05: MCP Server with Core Tools

## Objective

Implement the MCP server that exposes callmem tools over the Model Context Protocol. Wire up the core tools (`mem_session_start`, `mem_ingest`, `mem_search`, `mem_get_briefing`) to the engine.

## Files to create

- `src/callmem/mcp/__init__.py`
- `src/callmem/mcp/server.py` — MCP server entry point (stdio transport)
- `src/callmem/mcp/tools.py` — Tool definitions and handlers
- `src/callmem/mcp/resources.py` — Resource definitions (stubs for now)
- `tests/unit/test_mcp_tools.py`
- `tests/integration/__init__.py`
- `tests/integration/test_mcp_server.py`

## Files to modify

- `src/callmem/cli.py` — Wire `callmem serve` to launch MCP server
- `pyproject.toml` — Add `mcp` dependency (`mcp>=1.0`)

## Constraints

- Use the `mcp` Python SDK (official MCP SDK)
- stdio transport is the default and only required transport for v1
- Tool handlers call `MemoryEngine` methods — no direct database access
- All tool responses must be JSON-serializable
- Error handling: MCP errors with descriptive messages, never unhandled exceptions
- The server must handle the case where the database doesn't exist yet (auto-init)
- `mem_search` in this work order uses FTS5 only (retrieval engine comes in WO-07)

## Tool implementations

```python
# mem_session_start
async def handle_session_start(args):
    session = engine.start_session(
        agent_name=args.get("agent_name"),
        model_name=args.get("model_name")
    )
    # Briefing is a stub in this WO — just returns session info
    return {"session_id": session.id, "briefing": "Session started."}

# mem_ingest
async def handle_ingest(args):
    events = engine.ingest([EventInput(**e) for e in args["events"]])
    return {"ingested": len(events), "event_ids": [e.id for e in events]}

# mem_search (basic FTS5 for now)
async def handle_search(args):
    results = engine.search_fts(args["query"], limit=args.get("limit", 20))
    return {"results": [r.to_dict() for r in results]}

# mem_get_tasks
async def handle_get_tasks(args):
    tasks = engine.get_entities(type="todo", status=args.get("status", "open"))
    return {"tasks": [t.to_dict() for t in tasks]}

# mem_pin
async def handle_pin(args):
    entity = engine.set_pinned(args["entity_id"], args.get("pinned", True))
    return {"entity_id": entity.id, "pinned": entity.pinned}

# mem_session_end
async def handle_session_end(args):
    session = engine.end_session(engine.get_active_session().id, note=args.get("note"))
    return {"session_id": session.id, "status": session.status}
```

## Acceptance criteria

1. `python -m callmem.mcp.server --project /path` starts an MCP server on stdio
2. The server responds to MCP `tools/list` with all defined tools
3. `mem_session_start` creates a session and returns its ID
4. `mem_ingest` stores events and returns event IDs
5. `mem_search` returns FTS5 results for a query
6. `mem_get_tasks` returns entity list
7. `mem_pin` toggles pin status
8. `mem_session_end` closes the session
9. Error cases return MCP error responses (not crashes)
10. `pytest tests/unit/test_mcp_tools.py tests/integration/test_mcp_server.py` passes

## Suggested tests

```python
# Unit: test tool handlers directly
def test_ingest_handler(engine):
    result = handle_ingest(engine, {"events": [{"type": "prompt", "content": "test"}]})
    assert result["ingested"] == 1

# Integration: test via MCP protocol
async def test_mcp_tools_list(mcp_client):
    tools = await mcp_client.list_tools()
    tool_names = [t.name for t in tools]
    assert "mem_ingest" in tool_names
    assert "mem_search" in tool_names
    assert "mem_get_briefing" in tool_names

async def test_mcp_ingest_and_search(mcp_client):
    await mcp_client.call_tool("mem_session_start", {})
    await mcp_client.call_tool("mem_ingest", {
        "events": [{"type": "decision", "content": "Use Redis for caching"}]
    })
    result = await mcp_client.call_tool("mem_search", {"query": "Redis caching"})
    assert len(result["results"]) > 0
```

## OpenCode verification

After this WO, you should be able to add callmem to `opencode.json`:

```json
{
  "mcp": {
    "callmem": {
      "type": "local",
      "command": ["uv", "run", "python", "-m", "callmem.mcp.server", "--project", "."],
      "enabled": true
    }
  }
}
```

And see callmem tools in OpenCode's `/mcp` list.
