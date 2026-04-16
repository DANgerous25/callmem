"""MCP tool definitions for llm-mem.

Each tool is registered with the MCP server and delegates to the MemoryEngine.
Tool handlers are synchronous wrappers that catch exceptions and return
structured results.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from mcp.types import TextContent, Tool

if TYPE_CHECKING:
    from mcp.server import Server

    from llm_mem.core.engine import MemoryEngine


TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "mem_session_start",
        "description": (
            "Start a new memory session. "
            "Call this at the beginning of a coding session."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_name": {
                    "type": "string",
                    "description": "Name of the coding agent (e.g. 'opencode')",
                },
                "model_name": {
                    "type": "string",
                    "description": "Name of the LLM model being used",
                },
            },
        },
    },
    {
        "name": "mem_session_end",
        "description": "End the current memory session. Call this when finishing a coding session.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "note": {
                    "type": "string",
                    "description": "Optional summary note for the session",
                },
            },
        },
    },
    {
        "name": "mem_ingest",
        "description": "Store events in memory. Events are the raw stream of interactions.",
        "inputSchema": {
            "type": "object",
            "required": ["events"],
            "properties": {
                "events": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["type", "content"],
                        "properties": {
                            "type": {
                                "type": "string",
                                "enum": [
                                    "prompt", "response", "tool_call",
                                    "file_change", "decision", "todo",
                                    "failure", "discovery", "fact", "note",
                                ],
                            },
                            "content": {"type": "string"},
                            "metadata": {"type": "object"},
                        },
                    },
                    "description": "List of events to ingest",
                },
            },
        },
    },
    {
        "name": "mem_search",
        "description": "Search stored memories using full-text search.",
        "inputSchema": {
            "type": "object",
            "required": ["query"],
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum results to return (default 20)",
                    "default": 20,
                },
            },
        },
    },
    {
        "name": "mem_get_tasks",
        "description": "Get TODO entities from memory.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["open", "done", "cancelled"],
                    "description": "Filter by status (default: 'open')",
                    "default": "open",
                },
            },
        },
    },
    {
        "name": "mem_pin",
        "description": "Pin or unpin an entity to keep it prominent.",
        "inputSchema": {
            "type": "object",
            "required": ["entity_id"],
            "properties": {
                "entity_id": {
                    "type": "string",
                    "description": "ID of the entity to pin/unpin",
                },
                "pinned": {
                    "type": "boolean",
                    "description": "True to pin, False to unpin (default: True)",
                    "default": True,
                },
            },
        },
    },
]


def _make_result(data: dict[str, Any]) -> list[TextContent]:
    """Wrap a dict result as MCP TextContent."""
    return [TextContent(type="text", text=json.dumps(data))]


def _make_error(message: str) -> list[TextContent]:
    """Wrap an error message as MCP TextContent."""
    return [TextContent(type="text", text=json.dumps({"error": message}))]


def register_tools(server: Server, engine: MemoryEngine) -> None:
    """Register all llm-mem tools with the MCP server."""

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return [
            Tool(
                name=t["name"],
                description=t["description"],
                inputSchema=t["inputSchema"],
            )
            for t in TOOL_DEFINITIONS
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
        try:
            handler = _HANDLERS.get(name)
            if handler is None:
                return _make_error(f"Unknown tool: {name}")
            return handler(engine, arguments)
        except Exception as e:
            return _make_error(f"{type(e).__name__}: {e}")


# ── Tool handlers ────────────────────────────────────────────────────


def handle_session_start(engine: MemoryEngine, args: dict[str, Any]) -> list[TextContent]:
    session = engine.start_session(
        agent_name=args.get("agent_name"),
        model_name=args.get("model_name"),
    )
    return _make_result({
        "session_id": session.id,
        "briefing": "Session started.",
    })


def handle_session_end(engine: MemoryEngine, args: dict[str, Any]) -> list[TextContent]:
    active = engine.get_active_session()
    if active is None:
        return _make_error("No active session to end.")
    session = engine.end_session(active.id, note=args.get("note"))
    return _make_result({
        "session_id": session.id,
        "status": session.status,
    })


def handle_ingest(engine: MemoryEngine, args: dict[str, Any]) -> list[TextContent]:
    raw_events = args.get("events", [])
    events = [
        engine.ingest_one(
            type=e["type"],
            content=e["content"],
            metadata=e.get("metadata"),
        )
        for e in raw_events
    ]
    stored = [ev for ev in events if ev is not None]
    return _make_result({
        "ingested": len(stored),
        "event_ids": [e.id for e in stored],
    })


def handle_search(engine: MemoryEngine, args: dict[str, Any]) -> list[TextContent]:
    query = args.get("query", "")
    limit = args.get("limit", 20)
    results = engine.search_fts(query, limit=limit)
    return _make_result({"results": results})


def handle_get_tasks(engine: MemoryEngine, args: dict[str, Any]) -> list[TextContent]:
    status = args.get("status", "open")
    tasks = engine.get_entities(type="todo", status=status)
    return _make_result({"tasks": tasks})


def handle_pin(engine: MemoryEngine, args: dict[str, Any]) -> list[TextContent]:
    entity_id = args.get("entity_id", "")
    pinned = args.get("pinned", True)
    entity = engine.set_pinned(entity_id, pinned)
    return _make_result({
        "entity_id": entity["id"],
        "pinned": bool(entity.get("pinned", 0)),
    })


_HANDLERS: dict[str, Any] = {
    "mem_session_start": handle_session_start,
    "mem_session_end": handle_session_end,
    "mem_ingest": handle_ingest,
    "mem_search": handle_search,
    "mem_get_tasks": handle_get_tasks,
    "mem_pin": handle_pin,
}
