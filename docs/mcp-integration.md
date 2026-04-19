# MCP Integration

## Overview

callmem exposes itself as an MCP (Model Context Protocol) server. This is the primary integration surface — any MCP-capable client (OpenCode, Claude Code, Kilo, custom wrappers) can use callmem by adding it as an MCP server.

The MCP server provides three categories of capabilities:
1. **Tools** — Actions the agent can invoke (ingest, search, pin, etc.)
2. **Resources** — Read-only data the agent can reference (briefing, tasks, decisions)
3. **Prompts** — Prompt templates the agent can use (startup brief, memory review)

## Transport

callmem supports two MCP transports:

| Transport | When to use | How it works |
|---|---|---|
| **stdio** | OpenCode, Claude Code (default) | Parent process spawns callmem as a subprocess; communication over stdin/stdout |
| **SSE** | Remote access, multiple clients, web UI | callmem runs as an HTTP server; clients connect via Server-Sent Events |

Default: stdio. The MCP server auto-detects based on how it's launched.

## MCP Tools

### `mem_ingest`

Ingest one or more events into memory.

```json
{
  "name": "mem_ingest",
  "description": "Record an event in persistent memory. Call this to capture decisions, discoveries, failures, TODOs, or any notable information from the current session.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "events": {
        "type": "array",
        "items": {
          "type": "object",
          "properties": {
            "type": {
              "type": "string",
              "enum": ["prompt", "response", "tool_call", "file_change", "decision", "todo", "failure", "discovery", "fact", "note"]
            },
            "content": { "type": "string" },
            "metadata": {
              "type": "object",
              "description": "Optional. Keys: file_path, tool_name, priority, status, tags"
            }
          },
          "required": ["type", "content"]
        }
      }
    },
    "required": ["events"]
  }
}
```

Usage pattern: The agent calls this after making decisions, encountering errors, or discovering important information. For automatic capture, an adapter layer (see below) can hook into the agent's event stream and call `mem_ingest` transparently.

### `mem_search`

Search memory using keywords and/or structured filters.

```json
{
  "name": "mem_search",
  "description": "Search persistent memory for relevant information. Returns matching events, entities, and summaries ranked by relevance.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "query": { "type": "string", "description": "Free-text search query" },
      "types": {
        "type": "array",
        "items": { "type": "string", "enum": ["decision", "todo", "fact", "failure", "discovery", "event", "summary"] },
        "description": "Filter by entity/event types"
      },
      "session_id": { "type": "string", "description": "Limit to a specific session" },
      "limit": { "type": "integer", "default": 20 },
      "include_archived": { "type": "boolean", "default": false }
    },
    "required": ["query"]
  }
}
```

### `mem_get_briefing`

Get the startup context briefing for the current project.

```json
{
  "name": "mem_get_briefing",
  "description": "Get a compact briefing of what matters now — active TODOs, recent decisions, unresolved issues, and project context. Call this at the start of a session.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "max_tokens": { "type": "integer", "default": 2000, "description": "Token budget for the briefing" },
      "focus": { "type": "string", "description": "Optional focus area to prioritize in the briefing" }
    }
  }
}
```

### `mem_pin`

Pin or unpin an entity (prevents compaction, ensures it appears in briefings).

```json
{
  "name": "mem_pin",
  "description": "Pin an important memory so it is always included in briefings and never compacted.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "entity_id": { "type": "string" },
      "pinned": { "type": "boolean", "default": true }
    },
    "required": ["entity_id"]
  }
}
```

### `mem_get_tasks`

Get current TODOs and their status.

```json
{
  "name": "mem_get_tasks",
  "description": "List active TODOs and tasks from memory.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "status": {
        "type": "string",
        "enum": ["open", "done", "cancelled", "all"],
        "default": "open"
      },
      "limit": { "type": "integer", "default": 50 }
    }
  }
}
```

### `mem_update_task`

Update a TODO's status.

```json
{
  "name": "mem_update_task",
  "description": "Update the status of a TODO in memory.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "entity_id": { "type": "string" },
      "status": { "type": "string", "enum": ["open", "done", "cancelled"] },
      "note": { "type": "string", "description": "Optional note about the status change" }
    },
    "required": ["entity_id", "status"]
  }
}
```

### `mem_session_start`

Signal that a new session is starting (called automatically by adapter).

```json
{
  "name": "mem_session_start",
  "description": "Signal the start of a new coding session. Returns the startup briefing.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "agent_name": { "type": "string" },
      "model_name": { "type": "string" }
    }
  }
}
```

### `mem_session_end`

Signal session end (triggers summary generation).

```json
{
  "name": "mem_session_end",
  "description": "Signal the end of the current session. Triggers summary generation.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "note": { "type": "string", "description": "Optional session-end note" }
    }
  }
}
```

## MCP Resources

Resources are read-only data that the agent can reference in context.

| URI | Description | Update frequency |
|---|---|---|
| `memory://briefing` | Current startup briefing | On access (generated fresh) |
| `memory://tasks` | Active TODOs list | On access |
| `memory://decisions` | Recent decisions | On access |
| `memory://facts` | Pinned project facts | On access |
| `memory://session/current` | Current session summary so far | On access |

Resources are useful for agents that support MCP resource subscriptions — they can keep memory context up-to-date in the agent's context window automatically.

## MCP Prompts

Prompt templates that agents can use.

### `startup-brief`

```json
{
  "name": "startup-brief",
  "description": "System prompt addition with project memory context",
  "arguments": [
    { "name": "focus", "description": "Optional focus area", "required": false }
  ]
}
```

Returns a formatted system prompt block containing the briefing. Designed to be prepended to the agent's system prompt or first user message.

### `memory-review`

```json
{
  "name": "memory-review",
  "description": "Review and clean up current memory state",
  "arguments": []
}
```

Returns a prompt that guides the agent through reviewing pinned facts, active TODOs, and recent decisions — useful for periodic memory hygiene.

## OpenCode integration

### Configuration

Add to your project's `opencode.json` (or `~/.config/opencode/opencode.json` for global):

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "callmem": {
      "type": "local",
      "command": ["python", "-m", "callmem.mcp.server", "--project", "."],
      "enabled": true,
      "timeout": 10000
    }
  }
}
```

If using `uv` (recommended):

```json
{
  "mcp": {
    "callmem": {
      "type": "local",
      "command": ["uv", "run", "--directory", "/path/to/callmem", "python", "-m", "callmem.mcp.server", "--project", "."],
      "enabled": true
    }
  }
}
```

### AGENTS.md integration

Add to your project's `AGENTS.md` or `.opencode/agents.md`:

```markdown
## Memory

This project uses callmem for persistent memory across sessions.

At the start of each session:
1. Call `mem_session_start` to register the session and get a briefing
2. Read the briefing and incorporate it into your understanding

During the session:
- Call `mem_ingest` when you make important decisions, discover issues, or identify TODOs
- Call `mem_search` when you need to recall past context about a topic
- Call `mem_update_task` when you complete or cancel a TODO

At the end of the session:
- Call `mem_session_end` to trigger summary generation
```

### Automatic capture adapter

For fully automatic capture (no manual `mem_ingest` calls needed), callmem can optionally act as a proxy or event listener. The OpenCode adapter:

1. Subscribes to OpenCode's SSE event stream (`GET /event`)
2. Captures prompts, responses, and tool calls automatically
3. Feeds them to the ingest pipeline

This requires running callmem alongside OpenCode in "adapter mode":

```bash
callmem adapter opencode --opencode-url http://localhost:4096
```

This is a v1+ feature. In v0, ingest relies on explicit `mem_ingest` calls from the agent (guided by AGENTS.md instructions).

## Priority order for MCP tools

Build these tools in this order:

1. **`mem_session_start`** + **`mem_get_briefing`** — The core value proposition
2. **`mem_ingest`** — Must be able to store memories
3. **`mem_search`** — Must be able to retrieve them
4. **`mem_get_tasks`** — High-value structured query
5. **`mem_pin`** — User control over what persists
6. **`mem_update_task`** — Task lifecycle management
7. **`mem_session_end`** — Clean session close
8. Resources and prompts — Enhancement layer

## Future: non-MCP adapters

The MCP server calls the same core engine as any other adapter. To support non-MCP tools:

```
                    ┌─ MCP Server ──────────┐
                    │                        │
Agent ──────────────┤  MCP Tool Handler      ├──── Core Engine ──── SQLite
                    │                        │
                    └────────────────────────┘

                    ┌─ HTTP Adapter ─────────┐
                    │                        │
Custom wrapper ─────┤  REST API              ├──── Core Engine ──── SQLite
                    │                        │
                    └────────────────────────┘

                    ┌─ Python SDK ───────────┐
                    │                        │
Embedded use ───────┤  Direct function calls ├──── Core Engine ──── SQLite
                    │                        │
                    └────────────────────────┘
```

The `src/callmem/core/` module is the adapter-agnostic engine. MCP, REST, and direct Python calls all route through it.
