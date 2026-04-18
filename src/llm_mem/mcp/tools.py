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
        "name": "mem_get_briefing",
        "description": (
            "Get a startup briefing summarizing active TODOs, "
            "recent decisions, and project context."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "max_tokens": {
                    "type": "integer",
                    "description": "Maximum tokens for the briefing",
                },
                "focus": {
                    "type": "string",
                    "description": "Narrow briefing to a specific topic",
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
    {
        "name": "mem_search_index",
        "description": (
            "Layer 1 — Compact search index. Returns a compact table of "
            "matching entities for quick scanning. Start here, then use "
            "mem_timeline or mem_get_entities for more detail."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["query"],
            "properties": {
                "query": {
                    "type": "string",
                    "description": "FTS5 search query",
                },
                "type": {
                    "type": "string",
                    "description": "Filter by entity type",
                },
                "file_path": {
                    "type": "string",
                    "description": "Filter by associated file path",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum results (default 20)",
                    "default": 20,
                },
            },
        },
    },
    {
        "name": "mem_timeline",
        "description": (
            "Layer 2 — Chronological timeline. Returns entities around "
            "an anchor point with key_points for context."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "anchor_id": {
                    "type": "string",
                    "description": "Entity ID to center timeline around",
                },
                "depth_before": {
                    "type": "integer",
                    "description": "Entities before anchor (default 3)",
                    "default": 3,
                },
                "depth_after": {
                    "type": "integer",
                    "description": "Entities after anchor (default 3)",
                    "default": 3,
                },
            },
        },
    },
    {
        "name": "mem_get_entities",
        "description": (
            "Layer 3 — Full entity details. Returns complete content "
            "for specific entity IDs. Use after search_index/timeline."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["ids"],
            "properties": {
                "ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Entity IDs to fetch",
                },
            },
        },
    },
    {
        "name": "mem_search_by_file",
        "description": (
            "Find all memory entries related to a specific file."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["file_path"],
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "File path to search for",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum results (default 20)",
                    "default": 20,
                },
            },
        },
    },
    {
        "name": "mem_vault_review",
        "description": (
            "Mark a vault entry as a false positive, un-redacting the "
            "original content in the associated event."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["vault_id"],
            "properties": {
                "vault_id": {
                    "type": "string",
                    "description": "ID of the vault entry to mark as false positive",
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
    results = engine.search(query, limit=limit)
    return _make_result({"results": results})


def handle_get_briefing(engine: MemoryEngine, args: dict[str, Any]) -> list[TextContent]:
    briefing = engine.get_briefing(
        max_tokens=args.get("max_tokens"),
        focus=args.get("focus"),
    )
    return _make_result(briefing)


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


def handle_search_index(
    engine: MemoryEngine, args: dict[str, Any]
) -> list[TextContent]:
    query = args.get("query", "")
    entity_type = args.get("type")
    file_path = args.get("file_path")
    limit = args.get("limit", 20)

    if file_path:
        results = engine.repo.get_entities_by_file(file_path, limit=limit)
    else:
        results = engine.search(query, types=[entity_type] if entity_type else None, limit=limit)

    compact = []
    for r in results:
        eid = r.get("id", "")[:8]
        etype = r.get("type", "")
        title = r.get("title") or ""
        date = (r.get("timestamp") or r.get("created_at", ""))[:10]
        files = engine.repo.get_files_for_entity(r.get("id", ""))
        file_names = ", ".join(
            f["file_path"].rsplit("/", 1)[-1] for f in files[:3]
        )
        compact.append(
            f"#{eid} | {etype:10s} | {title[:40]:40s} | {date} | {file_names}"
        )

    header = "#ID     | Type       | Title                                    | Date       | Files"
    lines = [header] + compact
    return _make_result({"index": "\n".join(lines), "count": len(compact)})


def handle_timeline(
    engine: MemoryEngine, args: dict[str, Any]
) -> list[TextContent]:
    anchor_id = args.get("anchor_id")
    depth_before = args.get("depth_before", 3)
    depth_after = args.get("depth_after", 3)

    entities = engine.repo.get_timeline(
        engine.project_id,
        anchor_id=anchor_id,
        depth_before=depth_before,
        depth_after=depth_after,
    )

    from llm_mem.core.briefing import CATEGORY_EMOJI
    lines: list[str] = []
    for e in entities:
        eid = e.get("id", "")[:8]
        emoji = CATEGORY_EMOJI.get(e.get("type", ""), "")
        etype = e.get("type", "")
        title = e.get("title", "")
        kp = e.get("key_points") or ""
        anchor_marker = " [ANCHOR]" if e.get("id") == anchor_id else ""
        lines.append(f"#{eid} {emoji} {etype} — {title}{anchor_marker}")
        if kp:
            for point in kp.split("\n"):
                if point.strip():
                    lines.append(f"  {point.strip()}")

    return _make_result({"timeline": "\n".join(lines), "count": len(entities)})


def handle_get_entities(
    engine: MemoryEngine, args: dict[str, Any]
) -> list[TextContent]:
    ids = args.get("ids", [])
    results = []
    for eid in ids:
        conn = engine.db.connect()
        try:
            row = conn.execute(
                "SELECT * FROM entities WHERE id = ?", (eid,)
            ).fetchone()
        finally:
            conn.close()
        if row:
            from llm_mem.models.entities import Entity
            entity = Entity.from_row(dict(row))
            files = engine.repo.get_files_for_entity(eid)
            d = entity.to_row()
            d["files"] = files
            results.append(d)

    return _make_result({"entities": results, "count": len(results)})


def handle_search_by_file(
    engine: MemoryEngine, args: dict[str, Any]
) -> list[TextContent]:
    file_path = args.get("file_path", "")
    limit = args.get("limit", 20)
    results = engine.repo.get_entities_by_file(file_path, limit=limit)
    return _make_result({"entities": results, "count": len(results)})


def handle_vault_review(
    engine: MemoryEngine, args: dict[str, Any]
) -> list[TextContent]:
    vault_id = args.get("vault_id", "")
    if not vault_id:
        return _make_error("vault_id is required")
    try:
        engine.mark_false_positive(vault_id)
    except ValueError as exc:
        return _make_error(str(exc))
    return _make_result({"vault_id": vault_id, "status": "false_positive"})


_HANDLERS: dict[str, Any] = {
    "mem_session_start": handle_session_start,
    "mem_session_end": handle_session_end,
    "mem_ingest": handle_ingest,
    "mem_search": handle_search,
    "mem_get_briefing": handle_get_briefing,
    "mem_get_tasks": handle_get_tasks,
    "mem_pin": handle_pin,
    "mem_search_index": handle_search_index,
    "mem_timeline": handle_timeline,
    "mem_get_entities": handle_get_entities,
    "mem_search_by_file": handle_search_by_file,
    "mem_vault_review": handle_vault_review,
}
