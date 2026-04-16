# llm-mem

**Persistent memory for coding agents.**

llm-mem gives any LLM-powered coding tool a durable, searchable memory layer that survives across sessions. It captures what happened, compresses it in the background, and serves a compact briefing when you start a new session — so the agent picks up where you left off without manual context management.

## Why this exists

Coding agents today are stateless between sessions. Every time you open a new session, the agent forgets everything — decisions made, bugs fixed, architecture choices, TODOs, discovered gotchas. You end up re-explaining context, re-discovering the same things, and wasting tokens on information the agent already processed yesterday.

llm-mem fixes this by sitting between you and your coding agent as a transparent memory service.

## What it does

- **Startup briefing**: On session start, automatically provides a compact "here's what matters right now" context block — recent decisions, active TODOs, unresolved issues, project facts
- **Automatic capture**: During the session, ingests prompts, responses, tool calls, file changes, decisions, failures, and discoveries — no manual tagging required
- **Background compaction**: A local Ollama model summarizes, deduplicates, and compresses memories in the background, preventing unbounded growth
- **Retrieval**: On each prompt, retrieves relevant memories via structured queries + full-text search (semantic search optional in v2)
- **Inspectable**: A local web UI lets you browse, search, pin, edit, and delete memories

## Design principles

| Principle | What it means |
|---|---|
| **Model agnostic** | Works with any LLM provider — not tied to a specific vendor |
| **Agent agnostic** | Integrates via MCP first, but designed for non-MCP adapters too |
| **Local-first** | All data stays on your machine. SQLite database, local Ollama for summarization |
| **Privacy-friendly** | No cloud calls for memory management unless you opt in |
| **Zero manual overhead** | Capture and compaction happen automatically; user edits are optional |
| **Coding-project focused** | Optimized for long-lived software projects, not generic chatbot history |

## Why SQLite-first (not vector DB)

Most coding memory retrieval is structured:
- "What decisions did we make about the auth system?"
- "What TODOs are still open?"
- "What happened in the last 3 sessions?"

These queries are better served by structured tables + full-text search (FTS5) than by embedding similarity. SQLite is:
- Zero-dependency, single-file, trivially backed up
- Fast enough for tens of thousands of memories
- Portable across machines (just copy the `.db` file)
- Already has excellent full-text search via FTS5

Vector embeddings are a planned v2 enhancement for semantic retrieval when keyword search isn't enough. The schema is designed to accommodate them without migration pain.

## How Ollama is used

llm-mem uses a local Ollama model as its **memory-maintenance LLM** — separate from whatever model you use for interactive coding. This model handles:

- **Summarization**: Compressing verbose session logs into concise summaries
- **Entity extraction**: Pulling out decisions, facts, TODOs, and entities from raw events
- **Deduplication**: Identifying when new information supersedes old memories
- **Briefing generation**: Composing the startup context block

The default model is configurable (recommended: `qwen3:8b` or similar). The memory-maintenance model never touches your interactive coding context — it runs in background jobs.

## How OpenCode / MCP integration works

llm-mem exposes itself as an **MCP server** that OpenCode (or any MCP-capable client) connects to. The MCP server provides:

- **Tools**: `mem_ingest`, `mem_search`, `mem_get_briefing`, `mem_pin`, `mem_get_tasks`
- **Resources**: `memory://briefing`, `memory://tasks`, `memory://decisions`
- **Prompts**: `startup-brief` prompt template that agents can use at session start

OpenCode config (`opencode.json`):
```json
{
  "mcp": {
    "llm-mem": {
      "type": "local",
      "command": ["python", "-m", "llm_mem.mcp.server"],
      "enabled": true
    }
  }
}
```

See [docs/mcp-integration.md](docs/mcp-integration.md) for full details.

## Quick start

```bash
# Clone and set up
git clone https://github.com/DANgerous25/llm-mem.git
cd llm-mem

# Install with uv (recommended)
uv sync

# Or with pip
pip install -e ".[dev]"

# Ensure Ollama is running with a summarization model
ollama pull qwen3:8b

# Initialize a project memory store
llm-mem init --project ./my-project

# Start the MCP server (for OpenCode integration)
llm-mem serve

# Start the web UI
llm-mem ui
```

## Project structure

```
llm-mem/
├── src/llm_mem/
│   ├── core/           # Database, schema, memory engine
│   ├── mcp/            # MCP server and tool definitions
│   ├── ui/             # Local web UI (FastAPI + htmx)
│   ├── adapters/       # Integration adapters (OpenCode, future: Kilo, etc.)
│   └── models/         # Data models and types
├── docs/
│   ├── architecture.md
│   ├── schema.md
│   ├── mcp-integration.md
│   ├── roadmap.md
│   ├── ui.md
│   ├── config.md
│   ├── work-orders/    # Sequential implementation tickets
│   └── prompts/        # Coding agent prompt templates
├── tests/
├── scripts/
└── pyproject.toml
```

## Documentation

- [Architecture](docs/architecture.md) — System components, event flow, session lifecycle
- [Schema](docs/schema.md) — SQLite tables, indexes, FTS5 strategy
- [MCP Integration](docs/mcp-integration.md) — Tools, resources, OpenCode config
- [Roadmap](docs/roadmap.md) — v0 through v3 phased plan
- [Web UI](docs/ui.md) — Local inspection and management interface
- [Configuration](docs/config.md) — All configurable options
- [Sensitive Data](docs/sensitive-data.md) — Two-layer detection, encrypted vault, redaction pipeline
- [Coding Norms](docs/coding-norms.md) — Development standards and rationale
- [Getting Started](docs/getting-started.md) — First-session walkthrough for OpenCode/GLM
- [Work Orders](docs/work-orders/) — Step-by-step implementation tickets for coding agents
- [Prompts](docs/prompts/) — Templates for driving implementation with a coding agent

## Development Workflow

This project uses a bootstrap memory system while llm-mem itself is being built:

- **`AGENTS.md`** — Coding norms, session workflow, and architecture rules. Read by OpenCode/GLM automatically.
- **`.llm-mem/SESSION.md`** — What happened in the last session. Read at start, updated at end.
- **`.llm-mem/DECISIONS.md`** — Append-only design decision log.
- **`.llm-mem/TODO.md`** — Current task list and work order progress.

Helper scripts:
```bash
# Auto-generate session summary from git history
python scripts/session_save.py --from-git

# Interactive session summary
python scripts/session_save.py

# Display all memory files
python scripts/session_load.py
```

See [docs/coding-norms.md](docs/coding-norms.md) for the full rationale behind each norm.

## License

MIT
