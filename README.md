# llm-mem

**Persistent memory for LLM coding agents.**

llm-mem gives coding agents a durable, searchable memory that survives across sessions. It captures what happened, compresses it in the background using a local LLM, and serves a compact briefing when the next session starts — so the agent picks up where you left off without manual context management.

Inspired by [claude-mem](https://github.com/anthropics/claude-mem), but built from the ground up with a different philosophy: model-agnostic, local-first, and designed around pluggable LLM backends rather than a single vendor.

---

## Key Features

### Automatic Context at Startup
When a new session begins, llm-mem generates a structured briefing with context economics, an emoji-coded observation timeline, and a session summary. This is written to `SESSION_SUMMARY.md` in your project root, where your coding agent picks it up automatically — no manual context management needed.

### Real-Time Capture and Extraction
During the session, llm-mem ingests prompts, responses, tool calls, and file changes via SSE. A background worker runs entity extraction through your local LLM, pulling out decisions, facts, TODOs, bugs, features, and discoveries. New observations appear in the web UI within milliseconds via Server-Sent Events.

### Layered Compression
Raw events are compressed through multiple layers to keep token usage in check:
- **Entity extraction** — structured knowledge pulled from raw conversation
- **Chunk summaries** — rolling mid-session compression every N events
- **Session summaries** — structured wrap-up when a session ends (Investigated / Learned / Completed / Next Steps)
- **Cross-session summaries** — periodic project-level rollups
- **Compaction** — old events archived to keep the database lean

### Dual Content Views
Each observation has two representations:
- **Key Points** — bullet-point summary (~50-100 tokens), cheap for context injection
- **Synopsis** — flowing prose paragraph (~200-400 tokens), loaded on demand

### Pluggable LLM Backend
llm-mem doesn't care which model you use for coding. It uses a separate local model for memory maintenance:
- **Ollama** (recommended) — fully local, zero API cost, works offline
- **OpenAI-compatible** — any `/v1/chat/completions` API (LM Studio, vLLM, etc.)
- **None** — pattern-only mode when no LLM is available

### Web UI
A local web interface for browsing and managing memories:
- Card-based feed with colour-coded category badges
- Expandable cards with Key Points / Synopsis toggle
- Real-time updates via SSE
- Full-text search across all entities
- Session browser with event timeline
- Briefing preview showing exactly what the agent sees
- Accessible from your Tailscale network (default bind `0.0.0.0`)

### Sensitive Data Handling
Two-layer detection (pattern matching + LLM classification) catches secrets, credentials, and PII at ingest time. Detected items are redacted from memory and stored in an encrypted vault with configurable false-positive management.

### MCP Integration
Exposes tools via the Model Context Protocol so compatible agents can query memory on demand:
- `search` — full-text search across all observations
- `get_briefing` — generate a startup briefing
- `search_by_file` — find observations related to specific files
- `timeline` — chronological context around an observation

---

## Requirements

- **Python 3.11+**
- **Ollama** with a summarisation model (recommended: `qwen3:8b` or `qwen3:30b`)
- **Linux** (tested on Ubuntu/Debian x86_64 and ARM64)
- **SQLite 3.35+** (ships with Python 3.11+, FTS5 support required)

### Tested Environments

| Environment | Status |
|---|---|
| Ubuntu 24.04, x86_64 (Hetzner VPS) | Tested |
| Ubuntu 24.04, ARM64 (Hetzner CAX31) | Tested |
| Ollama with qwen3:8b | Tested |
| Ollama with qwen3:30b | Tested |
| OpenCode as coding agent | Tested |

macOS and Windows are untested but should work anywhere Python and Ollama run. The systemd service integration is Linux-only.

---

## Quick Start

### 1. Prerequisites

```bash
# Install Ollama (if not already installed)
curl -fsSL https://ollama.com/install.sh | sh

# Pull a summarisation model
ollama pull qwen3:8b
```

### 2. Install llm-mem

```bash
git clone https://github.com/DANgerous25/llm-mem.git
cd llm-mem

# Install with uv (recommended)
uv sync

# Or with pip
pip install -e .
```

### 3. Run the Setup Wizard

```bash
uv run llm-mem setup
```

The wizard walks you through:
- Choosing your LLM backend and model
- Setting the web UI port (with multi-project conflict detection)
- Configuring network bind address
- Importing existing OpenCode sessions from SQLite
- Optionally installing a systemd user service for auto-start

The setup script is safe to re-run — it reconfigures without wiping data and backs up your `config.toml` before changes.

### 4. Start the Daemon

```bash
# All-in-one: web UI + background workers + OpenCode SSE adapter
uv run llm-mem daemon

# Or via make
make daemon

# Or via systemd (if installed during setup)
make start
```

### 5. Configure Your Coding Agent

Add llm-mem as an MCP server in your agent's config. For OpenCode, add to `opencode.json`:

```json
{
  "mcp": {
    "llm-mem": {
      "type": "local",
      "command": ["uv", "run", "--directory", "/path/to/llm-mem", "python", "-m", "llm_mem.mcp.server"],
      "enabled": true
    }
  }
}
```

### 6. Open the Web UI

Navigate to `http://localhost:9090` (or your configured host:port).

---

## How It Works

```
┌─────────────────┐     SSE      ┌─────────────────┐     Extract     ┌──────────────┐
│  Coding Agent   │ ──────────▶  │   llm-mem       │ ──────────────▶ │  Local LLM   │
│  (OpenCode)     │              │   Adapter       │                 │  (Ollama)    │
└─────────────────┘              └────────┬────────┘                 └──────────────┘
                                          │
                                          ▼
                                 ┌─────────────────┐
                                 │   SQLite DB     │
                                 │   + FTS5        │
                                 └────────┬────────┘
                                          │
                              ┌───────────┼───────────┐
                              ▼           ▼           ▼
                        ┌──────────┐ ┌──────────┐ ┌──────────┐
                        │ Web UI   │ │ MCP      │ │ Briefing │
                        │ Feed     │ │ Server   │ │ Writer   │
                        └──────────┘ └──────────┘ └──────────┘
```

1. **Capture**: The adapter listens to your coding agent's event stream (SSE for OpenCode) and stores raw events
2. **Extract**: A background worker sends event batches to your local LLM for entity extraction — pulling out decisions, facts, TODOs, bugs, features, and discoveries
3. **Compress**: Summaries are generated at chunk, session, and cross-session levels
4. **Serve**: The briefing writer generates `SESSION_SUMMARY.md` in your project root; the MCP server responds to on-demand queries; the web UI shows everything in real-time

---

## CLI Reference

```bash
llm-mem setup              # Interactive setup wizard
llm-mem daemon             # Start UI + workers + adapter in one process
llm-mem ui                 # Start web UI only
llm-mem serve              # Start MCP server only
llm-mem import             # Import sessions from OpenCode SQLite DB
llm-mem status             # Show service status
llm-mem search <query>     # Search memories from the command line
llm-mem briefing           # Generate and print a briefing
```

---

## Entity Categories

llm-mem extracts these observation types, each with a colour-coded badge in the UI:

| Category | Icon | Description |
|----------|------|-------------|
| Feature | 🟢 | New functionality added |
| Bugfix | 🔴 | Bug identified and/or fixed |
| Discovery | 🔵 | Notable insight or finding |
| Decision | ⚖️ | Architectural or design choice |
| Todo | 📋 | Task to be done |
| Fact | 📝 | Durable project knowledge |
| Failure | ❌ | Error or failure encountered |
| Research | 🔬 | Investigation or analysis |
| Change | 🔄 | General code or file change |

---

## Configuration

All settings live in `.llm-mem/config.toml` in your project root. Key options:

```toml
[llm]
backend = "ollama"               # ollama | openai_compat | none
model = "qwen3:8b"
api_base = "http://localhost:11434"

[server]
host = "0.0.0.0"                 # Bind address (0.0.0.0 for Tailscale access)
port = 9090

[briefing]
max_tokens = 2000                # Token budget for startup briefing
auto_write_session_summary = true
session_summary_filename = "SESSION_SUMMARY.md"

[extraction]
batch_size = 10                  # Events per extraction batch
```

See [docs/config.md](docs/config.md) for the full reference.

---

## Why SQLite, Not a Vector DB?

Most coding memory retrieval is structured:
- "What decisions did we make about auth?"
- "What TODOs are still open?"
- "What happened in the last 3 sessions?"

These are better served by structured tables + full-text search (FTS5) than embedding similarity. SQLite is zero-dependency, single-file, trivially backed up, and fast enough for tens of thousands of memories.

Vector embeddings are a planned enhancement for semantic retrieval when keyword search isn't enough. The schema is designed to accommodate them without migration pain.

---

## Project Structure

```
llm-mem/
├── src/llm_mem/
│   ├── adapters/       # Agent integrations (OpenCode SSE, session import)
│   ├── core/           # Engine, extraction, briefing, compression, event bus
│   ├── mcp/            # MCP server and tool definitions
│   ├── models/         # Data models, config, entity types
│   └── ui/             # Web UI (FastAPI + Jinja2 + htmx + SSE)
├── scripts/            # Setup wizard, session helpers
├── tests/              # 382+ tests (unit + integration)
├── docs/
│   ├── work-orders/    # Implementation tickets
│   └── ...             # Architecture, schema, config, roadmap docs
└── pyproject.toml
```

---

## Development

```bash
# Install with dev dependencies
uv sync --extra dev

# Run tests
make test

# Run linter
make lint

# Run both
make check
```

---

## Roadmap

See [docs/roadmap.md](docs/roadmap.md) for the full plan. Highlights:

- **Progressive disclosure search** — 3-layer MCP search pattern (index → timeline → full details)
- **File-level tracking** — associate observations with specific files
- **Knowledge agents** — build queryable corpora from observation history
- **Settings panel** — web UI for config with live briefing preview
- **Vector search** — optional semantic retrieval via sentence-transformers

---

## Acknowledgements

llm-mem was inspired by [claude-mem](https://github.com/anthropics/claude-mem) by Alex Newman. We share the same goal — giving coding agents persistent memory — but llm-mem is built from scratch with a focus on model-agnostic operation, local-first architecture, and pluggable LLM backends.

---

## License

[MIT](LICENSE)
