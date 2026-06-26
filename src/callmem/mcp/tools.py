"""MCP tool definitions for callmem.

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

    from callmem.core.engine import MemoryEngine


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
                "include_stale": {
                    "type": "boolean",
                    "description": (
                        "Include entities flagged as stale. Defaults to "
                        "false so superseded/contradicted items don't "
                        "pollute results."
                    ),
                    "default": False,
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
            "for specific entity IDs. Use after search_index/timeline "
            "or to resolve the short IDs shown in the startup briefing. "
            "Accepts full ULIDs or the 8-char short IDs from the "
            "briefing (with or without leading '#')."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["ids"],
            "properties": {
                "ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Entity IDs to fetch. Full ULIDs or the short "
                        "form shown in briefings (e.g. 'F5AVDQ25')."
                    ),
                },
            },
        },
    },
    {
        "name": "mem_check_context",
        "description": (
            "Advisory check for long sessions: returns whether you "
            "should compress older context. Call every ~30 messages "
            "with your approximate message count (and estimated "
            "tokens if you can compute them). Response 'status' is "
            "'ok' or 'compress_recommended'."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["message_count"],
            "properties": {
                "message_count": {
                    "type": "integer",
                    "description": "Approximate messages in this session",
                },
                "estimated_tokens": {
                    "type": "integer",
                    "description": (
                        "Optional — estimated token usage so far "
                        "(improves the recommendation)."
                    ),
                    "default": 0,
                },
            },
        },
    },
    {
        "name": "mem_compress_context",
        "description": (
            "Record a summary of older conversation you are about to "
            "drop from your context. callmem stores it as a chunk "
            "summary and returns a short marker to drop in place of "
            "the compressed exchanges."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["summary"],
            "properties": {
                "summary": {
                    "type": "string",
                    "description": (
                        "Your summary of what was compressed — "
                        "preserve decisions, TODOs, and failures "
                        "verbatim."
                    ),
                },
                "message_range": {
                    "type": "string",
                    "description": (
                        "Optional human-readable range, e.g. "
                        "'messages 1-80'"
                    ),
                    "default": "",
                },
            },
        },
    },
    {
        "name": "mem_file_context",
        "description": (
            "Return callmem's observation timeline for a file path. "
            "Call this before re-reading a file you've worked on — "
            "if the timeline covers your task, skip the raw read "
            "(typically ~95% token savings)."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["path"],
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path (relative or absolute)",
                },
                "include_content": {
                    "type": "boolean",
                    "description": (
                        "If true, also return the file's current "
                        "on-disk content."
                    ),
                    "default": False,
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
    {
        "name": "mem_mark_stale",
        "description": (
            "Flag an entity as stale so it stops appearing in briefings "
            "and search. Use when a decision/fact/TODO has been "
            "superseded or no longer reflects the project."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["entity_id", "reason"],
            "properties": {
                "entity_id": {
                    "type": "string",
                    "description": "Entity to mark as stale",
                },
                "reason": {
                    "type": "string",
                    "enum": ["superseded", "contradicted", "outdated", "manual"],
                    "description": "Why the entity is stale",
                },
                "superseded_by": {
                    "type": "string",
                    "description": "Optional ID of the entity that replaces this one",
                },
            },
        },
    },
    {
        "name": "mem_mark_current",
        "description": (
            "Clear a previously-set stale flag. Use to undo a "
            "false-positive staleness decision."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["entity_id"],
            "properties": {
                "entity_id": {
                    "type": "string",
                    "description": "Entity to unmark",
                },
            },
        },
    },
    {
        "name": "mem_task_create",
        "description": (
            "Create a task (optionally with parent_id for subtasks). "
            "Tasks form a structured tree that survives context resets."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["title"],
            "properties": {
                "title": {"type": "string", "description": "Task title"},
                "parent_id": {"type": "string", "description": "Parent task ID for subtasks"},
                "session_id": {"type": "string", "description": "Session to attach task to"},
                "description": {"type": "string", "description": "Detailed task description"},
                "task_type": {
                    "type": "string",
                    "description": "coding, reasoning, summarization, analysis, design, etc.",
                },
                "complexity_hint": {
                    "type": "integer",
                    "description": "0-10 user override of complexity assessment",
                },
            },
        },
    },
    {
        "name": "mem_task_update",
        "description": (
            "Update a task's status, result, cost, tokens, model assignment, "
            "or other fields."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["task_id"],
            "properties": {
                "task_id": {"type": "string", "description": "Task ID to update"},
                "status": {
                    "type": "string",
                    "enum": ["pending", "in_progress", "completed", "failed", "cancelled"],
                    "description": "New task status",
                },
                "model_assigned": {"type": "string", "description": "Which model was assigned"},
                "model_reason": {"type": "string", "description": "Why that model was chosen"},
                "eval_score": {"type": "number", "description": "0.0-1.0 quality score"},
                "eval_feedback": {"type": "string", "description": "Judge's feedback text"},
                "cost_usd": {"type": "number", "description": "Actual cost incurred"},
                "tokens_input": {"type": "integer", "description": "Input tokens used"},
                "tokens_output": {"type": "integer", "description": "Output tokens used"},
                "result_ref": {
                    "type": "string",
                    "description": "Reference to result (entity/event ID)",
                },
                "description": {"type": "string", "description": "Updated description"},
                "title": {"type": "string", "description": "Updated title"},
            },
        },
    },
    {
        "name": "mem_task_list",
        "description": "List tasks with filters (status, parent, session, type).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["pending", "in_progress", "completed", "failed", "cancelled"],
                },
                "parent_id": {"type": "string", "description": "Filter by parent task"},
                "session_id": {"type": "string", "description": "Filter by session"},
                "task_type": {"type": "string", "description": "Filter by task type"},
                "limit": {"type": "integer", "default": 100},
            },
        },
    },
    {
        "name": "mem_task_tree",
        "description": "Get the full task tree from a root task (all descendants).",
        "inputSchema": {
            "type": "object",
            "required": ["root_id"],
            "properties": {
                "root_id": {"type": "string", "description": "Root task ID"},
            },
        },
    },
    {
        "name": "mem_model_stats",
        "description": (
            "Query aggregated model performance stats. Filter by model "
            "name and/or task type."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "model_name": {"type": "string", "description": "Filter by model name"},
                "task_type": {"type": "string", "description": "Filter by task type"},
            },
        },
    },
    {
        "name": "mem_model_compare",
        "description": "Compare two or more models on a task type.",
        "inputSchema": {
            "type": "object",
            "required": ["model_names"],
            "properties": {
                "model_names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Model names to compare",
                },
                "task_type": {"type": "string", "description": "Task type to compare on"},
            },
        },
    },
    {
        "name": "mem_eval",
        "description": (
            "Record an evaluation for an event or entity. "
            "Score is 0.0-1.0, with optional feedback and evaluator model."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["id", "score"],
            "properties": {
                "id": {"type": "string", "description": "Event or entity ID"},
                "score": {"type": "number", "description": "Quality score 0.0-1.0"},
                "feedback": {"type": "string", "description": "Evaluator's feedback text"},
                "evaluator_model": {"type": "string", "description": "Which model did the eval"},
                "target_type": {
                    "type": "string",
                    "enum": ["event", "entity"],
                    "description": (
                        "Whether the ID is an event or entity "
                        "(auto-detected if omitted)"
                    ),
                },
            },
        },
    },
    {
        "name": "mem_eval_summary",
        "description": "Get evaluation statistics (avg score by type, model).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "entity_type": {"type": "string", "description": "Filter by entity type"},
                "model_name": {"type": "string", "description": "Filter by eval model"},
            },
        },
    },
    {
        "name": "mem_compile_context",
        "description": (
            "Compile a context payload optimized for a target model with "
            "a specific token budget. Returns system_context text ready "
            "to prepend to a model's prompt."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["target_model"],
            "properties": {
                "target_model": {
                    "type": "string",
                    "description": "Model name (for context window lookup)",
                },
                "token_budget": {
                    "type": "integer",
                    "description": "Max tokens for compiled context",
                },
                "focus": {"type": "string", "description": "Narrow to a topic or task"},
                "include_tasks": {
                    "type": "boolean",
                    "description": "Include open task tree",
                    "default": False,
                },
                "include_files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Include file context for specific files",
                },
                "detail_level": {
                    "type": "string",
                    "enum": ["brief", "standard", "full"],
                    "default": "standard",
                    "description": (
                        "Level of detail: brief (key_points), "
                        "standard (synopsis), full (complete)"
                    ),
                },
            },
        },
    },
    {
        "name": "mem_model_list",
        "description": (
            "List known models with filters (provider, capability, "
            "max_price, geo_region, quality_tier, gateway)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "provider": {"type": "string"},
                "quality_tier": {
                    "type": "string",
                    "enum": ["frontier", "strong", "standard", "budget", "legacy"],
                },
                "max_price": {"type": "number", "description": "Max USD per 1M input tokens"},
                "require_tools": {"type": "boolean", "default": False},
                "require_vision": {"type": "boolean", "default": False},
                "geo_region": {"type": "string", "description": "ISO region code"},
                "gateway": {"type": "string", "description": "Filter by gateway name"},
                "limit": {"type": "integer", "default": 100},
            },
        },
    },
    {
        "name": "mem_model_info",
        "description": "Get full info for a specific model.",
        "inputSchema": {
            "type": "object",
            "required": ["model_name"],
            "properties": {
                "model_name": {
                    "type": "string",
                    "description": "Model name (e.g. 'anthropic/claude-sonnet-4')",
                },
            },
        },
    },
    {
        "name": "mem_model_recommend",
        "description": (
            "Given a task_type and optional constraints, return ranked "
            "model recommendations combining static benchmarks + observed "
            "performance + geo-availability."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["task_type"],
            "properties": {
                "task_type": {
                    "type": "string",
                    "description": "coding, reasoning, summarization, etc.",
                },
                "geo_region": {"type": "string", "description": "User's ISO region code"},
                "max_cost": {"type": "number", "description": "Max USD per 1M input tokens"},
                "min_context": {"type": "integer", "description": "Minimum context window size"},
                "require_tools": {"type": "boolean", "default": False},
                "require_gateway": {"type": "string", "description": "Required gateway name"},
            },
        },
    },
    {
        "name": "mem_model_geo_check",
        "description": (
            "Given a model and user's region, return availability "
            "and which gateway(s) can serve it."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["model_name", "region"],
            "properties": {
                "model_name": {"type": "string"},
                "region": {"type": "string", "description": "ISO region code (e.g. 'US', 'EU')"},
            },
        },
    },
    {
        "name": "mem_model_refresh",
        "description": "Trigger a re-sync and research update for a specific model or all models.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "model_name": {
                    "type": "string",
                    "description": "Specific model to refresh (all if omitted)",
                },
            },
        },
    },
    {
        "name": "mem_rewind_create",
        "description": (
            "Create a rewind point (snapshot current state). "
            "Use before risky operations so you can undo."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "label": {
                    "type": "string",
                    "description": "User-provided label for the rewind point",
                },
            },
        },
    },
    {
        "name": "mem_rewind_list",
        "description": "List available rewind points.",
        "inputSchema": {
            "type": "object",
        },
    },
    {
        "name": "mem_rewind_restore",
        "description": (
            "Restore to a rewind point. Soft-archives everything created "
            "after it (does not hard-delete)."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["rewind_id"],
            "properties": {
                "rewind_id": {"type": "string", "description": "Rewind point ID to restore to"},
            },
        },
    },
    {
        "name": "mem_rewind_diff",
        "description": "Show what would change if restored to a given rewind point.",
        "inputSchema": {
            "type": "object",
            "required": ["rewind_id"],
            "properties": {
                "rewind_id": {"type": "string", "description": "Rewind point ID"},
            },
        },
    },
    {
        "name": "mem_set_overview",
        "description": (
            "Set the project overview — a manually-authored summary that "
            "appears at the top of every briefing. Writing a new overview "
            "replaces the previous one. This is the primary way agents "
            "record what the project IS, its architecture, status, and "
            "next steps."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["content"],
            "properties": {
                "content": {
                    "type": "string",
                    "description": (
                        "Markdown overview text. Truncated to ~500 tokens "
                        "in briefings."
                    ),
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
    """Register all callmem tools with the MCP server."""

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
    include_stale = bool(args.get("include_stale", False))
    results = engine.search(query, limit=limit, include_stale=include_stale)
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

    from callmem.core.briefing import CATEGORY_EMOJI
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
    for raw_eid in ids:
        eid = (raw_eid or "").lstrip("#").strip()
        if not eid:
            continue
        row = engine.repo.get_entity(eid)
        if row is None and len(eid) < 26:
            row = engine.repo.get_entity_by_short_id(eid)
        if row:
            from callmem.models.entities import Entity
            entity = Entity.from_row(row)
            full_id = row["id"]
            files = engine.repo.get_files_for_entity(full_id)
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


def handle_file_context(
    engine: MemoryEngine, args: dict[str, Any]
) -> list[TextContent]:
    path = args.get("path", "")
    if not path:
        return _make_error("path is required")
    include_content = bool(args.get("include_content", False))
    data = engine.get_file_context(path, include_content=include_content)
    return _make_result(data)


def handle_check_context(
    engine: MemoryEngine, args: dict[str, Any]
) -> list[TextContent]:
    message_count = int(args.get("message_count", 0))
    estimated_tokens = int(args.get("estimated_tokens", 0))
    data = engine.check_context(
        message_count=message_count,
        estimated_tokens=estimated_tokens,
    )
    return _make_result(data)


def handle_compress_context(
    engine: MemoryEngine, args: dict[str, Any]
) -> list[TextContent]:
    summary = args.get("summary", "")
    if not summary:
        return _make_error("summary is required")
    try:
        data = engine.compress_context(
            summary=summary,
            message_range=args.get("message_range", ""),
        )
    except ValueError as exc:
        return _make_error(str(exc))
    return _make_result(data)


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


def handle_mark_stale(
    engine: MemoryEngine, args: dict[str, Any],
) -> list[TextContent]:
    entity_id = args.get("entity_id", "")
    reason = args.get("reason", "manual")
    if not entity_id:
        return _make_error("entity_id is required")
    entity = engine.mark_stale(
        entity_id, reason=reason,
        superseded_by=args.get("superseded_by"),
    )
    if entity is None:
        return _make_error(f"Entity not found: {entity_id}")
    return _make_result({
        "entity_id": entity_id,
        "stale": bool(entity.get("stale", 0)),
        "reason": entity.get("staleness_reason"),
        "superseded_by": entity.get("superseded_by"),
    })


def handle_mark_current(
    engine: MemoryEngine, args: dict[str, Any],
) -> list[TextContent]:
    entity_id = args.get("entity_id", "")
    if not entity_id:
        return _make_error("entity_id is required")
    entity = engine.mark_current(entity_id)
    if entity is None:
        return _make_error(f"Entity not found: {entity_id}")
    return _make_result({
        "entity_id": entity_id,
        "stale": bool(entity.get("stale", 0)),
    })


# ── Task graph handlers (A1) ────────────────────────────────────────


def handle_task_create(engine: MemoryEngine, args: dict[str, Any]) -> list[TextContent]:
    try:
        task = engine.create_task(
            title=args["title"],
            parent_id=args.get("parent_id"),
            session_id=args.get("session_id"),
            description=args.get("description"),
            task_type=args.get("task_type"),
            complexity_hint=args.get("complexity_hint"),
        )
    except KeyError:
        return _make_error("title is required")
    return _make_result(task)


def handle_task_update(engine: MemoryEngine, args: dict[str, Any]) -> list[TextContent]:
    task_id = args.get("task_id", "")
    if not task_id:
        return _make_error("task_id is required")
    fields: dict[str, Any] = {}
    for key in (
        "status", "model_assigned", "model_reason", "eval_score",
        "eval_feedback", "cost_usd", "tokens_input", "tokens_output",
        "result_ref", "description", "title",
    ):
        if key in args:
            fields[key] = args[key]
    try:
        task = engine.update_task(task_id, fields)
    except ValueError as exc:
        return _make_error(str(exc))
    return _make_result(task)


def handle_task_list(engine: MemoryEngine, args: dict[str, Any]) -> list[TextContent]:
    tasks = engine.list_tasks(
        status=args.get("status"),
        parent_id=args.get("parent_id"),
        session_id=args.get("session_id"),
        task_type=args.get("task_type"),
        limit=args.get("limit", 100),
    )
    return _make_result({"tasks": tasks, "count": len(tasks)})


def handle_task_tree(engine: MemoryEngine, args: dict[str, Any]) -> list[TextContent]:
    root_id = args.get("root_id", "")
    if not root_id:
        return _make_error("root_id is required")
    tree = engine.get_task_tree(root_id)
    return _make_result({"tree": tree, "count": len(tree)})


# ── Model stats handlers (A2) ───────────────────────────────────────


def handle_model_stats(engine: MemoryEngine, args: dict[str, Any]) -> list[TextContent]:
    stats = engine.query_model_stats(
        model_name=args.get("model_name"),
        task_type=args.get("task_type"),
    )
    return _make_result({"stats": stats, "count": len(stats)})


def handle_model_compare(engine: MemoryEngine, args: dict[str, Any]) -> list[TextContent]:
    model_names = args.get("model_names", [])
    if not model_names:
        return _make_error("model_names is required")
    comparison = engine.compare_models(
        model_names=model_names,
        task_type=args.get("task_type"),
    )
    return _make_result({"comparison": comparison})


# ── Eval handlers (A3) ──────────────────────────────────────────────


def handle_eval(engine: MemoryEngine, args: dict[str, Any]) -> list[TextContent]:
    target_id = args.get("id", "")
    if not target_id:
        return _make_error("id is required")
    score = args.get("score")
    if score is None:
        return _make_error("score is required")

    target_type = args.get("target_type")
    if target_type is None:
        event = engine.get_event(target_id)
        target_type = "event" if event else "entity"

    try:
        if target_type == "event":
            result = engine.eval_event(
                target_id,
                score=float(score),
                feedback=args.get("feedback"),
                evaluator_model=args.get("evaluator_model"),
            )
        else:
            result = engine.eval_entity(
                target_id,
                score=float(score),
                feedback=args.get("feedback"),
            )
    except ValueError as exc:
        return _make_error(str(exc))
    return _make_result(result)


def handle_eval_summary(engine: MemoryEngine, args: dict[str, Any]) -> list[TextContent]:
    summary = engine.eval_summary(
        entity_type=args.get("entity_type"),
        model_name=args.get("model_name"),
    )
    return _make_result(summary)


# ── Context compilation handler (A4) ────────────────────────────────


def handle_compile_context(engine: MemoryEngine, args: dict[str, Any]) -> list[TextContent]:
    target_model = args.get("target_model", "")
    if not target_model:
        return _make_error("target_model is required")
    result = engine.compile_context(
        target_model=target_model,
        token_budget=args.get("token_budget"),
        focus=args.get("focus"),
        include_tasks=bool(args.get("include_tasks", False)),
        include_files=args.get("include_files"),
        detail_level=args.get("detail_level", "standard"),
    )
    return _make_result(result)


# ── Model registry handlers (A5) ────────────────────────────────────


def handle_model_list(engine: MemoryEngine, args: dict[str, Any]) -> list[TextContent]:
    models = engine.list_models(
        provider=args.get("provider"),
        quality_tier=args.get("quality_tier"),
        max_price=args.get("max_price"),
        require_tools=bool(args.get("require_tools", False)),
        require_vision=bool(args.get("require_vision", False)),
        geo_region=args.get("geo_region"),
        gateway=args.get("gateway"),
        limit=args.get("limit", 100),
    )
    return _make_result({"models": models, "count": len(models)})


def handle_model_info(engine: MemoryEngine, args: dict[str, Any]) -> list[TextContent]:
    model_name = args.get("model_name", "")
    if not model_name:
        return _make_error("model_name is required")
    info = engine.get_model_info(model_name)
    if info is None:
        return _make_error(f"Model not found: {model_name}")
    return _make_result(info)


def handle_model_recommend(engine: MemoryEngine, args: dict[str, Any]) -> list[TextContent]:
    task_type = args.get("task_type", "")
    if not task_type:
        return _make_error("task_type is required")
    recommendations = engine.recommend_model(
        task_type=task_type,
        geo_region=args.get("geo_region"),
        max_cost=args.get("max_cost"),
        min_context=args.get("min_context"),
        require_tools=bool(args.get("require_tools", False)),
        require_gateway=args.get("require_gateway"),
    )
    return _make_result({"recommendations": recommendations})


def handle_model_geo_check(engine: MemoryEngine, args: dict[str, Any]) -> list[TextContent]:
    model_name = args.get("model_name", "")
    region = args.get("region", "")
    if not model_name or not region:
        return _make_error("model_name and region are required")
    result = engine.check_model_geo(model_name, region)
    return _make_result(result)


def handle_model_refresh(engine: MemoryEngine, args: dict[str, Any]) -> list[TextContent]:
    result = engine.refresh_model(args.get("model_name"))
    return _make_result(result)


# ── Rewind handlers (A6) ────────────────────────────────────────────


def handle_rewind_create(engine: MemoryEngine, args: dict[str, Any]) -> list[TextContent]:
    rp = engine.create_rewind_point(label=args.get("label"))
    return _make_result(rp)


def handle_rewind_list(engine: MemoryEngine, args: dict[str, Any]) -> list[TextContent]:
    points = engine.list_rewind_points()
    return _make_result({"rewind_points": points, "count": len(points)})


def handle_rewind_restore(engine: MemoryEngine, args: dict[str, Any]) -> list[TextContent]:
    rewind_id = args.get("rewind_id", "")
    if not rewind_id:
        return _make_error("rewind_id is required")
    try:
        result = engine.restore_rewind_point(rewind_id)
    except ValueError as exc:
        return _make_error(str(exc))
    return _make_result(result)


def handle_rewind_diff(engine: MemoryEngine, args: dict[str, Any]) -> list[TextContent]:
    rewind_id = args.get("rewind_id", "")
    if not rewind_id:
        return _make_error("rewind_id is required")
    try:
        result = engine.get_rewind_diff(rewind_id)
    except ValueError as exc:
        return _make_error(str(exc))
    return _make_result(result)


# ── Project overview handler ────────────────────────────────────────


def handle_set_overview(engine: MemoryEngine, args: dict[str, Any]) -> list[TextContent]:
    content = args.get("content", "")
    if not content.strip():
        return _make_error("content is required")
    row = engine.set_overview(content)
    return _make_result({
        "project_id": row["project_id"],
        "updated_at": row["updated_at"],
        "content_length": len(content),
    })


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
    "mem_file_context": handle_file_context,
    "mem_check_context": handle_check_context,
    "mem_compress_context": handle_compress_context,
    "mem_vault_review": handle_vault_review,
    "mem_mark_stale": handle_mark_stale,
    "mem_mark_current": handle_mark_current,
    "mem_task_create": handle_task_create,
    "mem_task_update": handle_task_update,
    "mem_task_list": handle_task_list,
    "mem_task_tree": handle_task_tree,
    "mem_model_stats": handle_model_stats,
    "mem_model_compare": handle_model_compare,
    "mem_eval": handle_eval,
    "mem_eval_summary": handle_eval_summary,
    "mem_compile_context": handle_compile_context,
    "mem_model_list": handle_model_list,
    "mem_model_info": handle_model_info,
    "mem_model_recommend": handle_model_recommend,
    "mem_model_geo_check": handle_model_geo_check,
    "mem_model_refresh": handle_model_refresh,
    "mem_rewind_create": handle_rewind_create,
    "mem_rewind_list": handle_rewind_list,
    "mem_rewind_restore": handle_rewind_restore,
    "mem_rewind_diff": handle_rewind_diff,
    "mem_set_overview": handle_set_overview,
}
