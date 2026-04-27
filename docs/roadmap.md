# Roadmap

## v0: Repo scaffold and contracts

**Goal**: Establish the project skeleton, data models, schema, and interfaces so that implementation tasks can proceed independently.

| Deliverable | Status |
|---|---|
| Repository structure with `src/callmem/` package | This document |
| `pyproject.toml` with dependencies | This document |
| SQLite schema DDL (`001_initial.sql`) | This document |
| Database initialization and migration runner | v0 |
| Core data models (Pydantic) | v0 |
| Core engine interface (abstract methods) | v0 |
| MCP server entry point (no-op tools) | v0 |
| CLI skeleton (`callmem init`, `callmem serve`) | v0 |
| Test infrastructure (pytest, fixtures) | v0 |
| CI basics (linting, type checks) | v0 |

**Exit criteria**: `callmem init` creates a database, `callmem serve` starts an MCP server that responds to tool listing, all tests pass.

## v1: Local ingest + startup brief + search + UI

**Goal**: A usable memory system. An agent can store memories, get briefings, and search. A human can inspect and edit via web UI.

### v1.0 — Ingest and storage
- Implement `mem_ingest` tool with event normalization
- Implement session management (`mem_session_start`, `mem_session_end`)
- Write events and sessions to SQLite
- FTS5 index population via triggers

### v1.1 — Entity extraction
- Background worker: extract decisions, TODOs, facts, failures from raw events
- Ollama integration for extraction prompts
- Queue system (simple SQLite-backed job queue — no external dependency)
- Store extracted entities in `entities` table

### v1.2 — Summarization
- Event-level summaries for verbose events
- Chunk-level summaries (every N events)
- Session-level summaries on session end
- Summary token budgeting

### v1.3 — Retrieval and briefing
- Implement retrieval engine: structured + FTS5 + recency
- Implement `mem_search` tool
- Implement `mem_get_briefing` tool
- Briefing assembly with token budget
- Implement `mem_get_tasks` tool

### v1.4 — Compaction
- Compaction worker with age-based policies
- Configurable retention thresholds
- Compaction logging
- Pinned-item protection

### v1.5 — Web UI
- FastAPI backend serving htmx pages
- Session browser (list sessions, view events)
- Memory search interface
- Entity viewer (TODOs, decisions, facts)
- Pin/unpin, edit, delete entities
- Compaction log viewer
- Settings page

### v1.6 — OpenCode adapter
- AGENTS.md template with memory instructions
- OpenCode SSE event listener for automatic capture
- `callmem adapter opencode` command

**Exit criteria**: Full workflow works end-to-end — start OpenCode session, work on code, memories captured automatically, close session, start new session, get briefing with yesterday's context. Web UI lets you inspect everything.

## v2: Optional embeddings and semantic retrieval

**Goal**: Add vector-based semantic search as an optional retrieval strategy.

### v2.0 — Embeddings infrastructure
- `embeddings` table creation (migration)
- Pluggable embedding backend interface
- Local backend: `sentence-transformers` via Ollama or Python
- API backend: OpenAI embeddings (optional)

### v2.1 — Embedding generation
- Background worker: embed entities and summaries
- Incremental embedding (only new/changed items)
- Embedding model configuration

### v2.2 — Semantic retrieval
- Vector similarity search (cosine distance)
- Integration with retrieval engine as a fourth strategy
- Configurable weight in result ranking
- sqlite-vec or FAISS for efficient nearest-neighbor lookup

### v2.3 — Hybrid search
- Combined scoring: BM25 + vector similarity + recency + type priority
- Auto-tuning of weights based on result feedback
- Search quality metrics

**Exit criteria**: `mem_search` with `semantic=true` returns relevant results even when keywords don't match. Embeddings are generated in the background without blocking ingest or retrieval.

## v3: Smarter memory policies / multi-project / policy engine

**Goal**: Production-grade memory management for power users with multiple projects.

### v3.0 — Multi-project
- Project switching in CLI and MCP
- Cross-project search
- Project-specific configuration
- Shared facts across projects (global knowledge)

### v3.1 — Policy engine
- Configurable retention policies per entity type
- Importance scoring model (trainable or rule-based)
- Automatic priority adjustment based on access patterns
- "Memory pressure" system: more aggressive compaction when DB grows large

### v3.2 — Memory graph
- Traverse `memory_edges` for related context
- "Why was this decided?" tracing
- Visual graph in web UI

### v3.3 — Advanced compaction
- LLM-driven merge: combine related entities into single refined entities
- Contradiction detection: flag when new info contradicts stored facts
- Confidence scoring on extracted entities
- User review queue for low-confidence extractions

### v3.4 — Export and portability
- Export project memory as portable format (JSON/SQLite dump)
- Import from other memory systems
- Memory snapshots and restore points

**Exit criteria**: Multiple projects managed from a single callmem instance with independent memory stores. Policy engine automatically manages retention without user intervention.

## Backlog (unscheduled)

- **Team memory**: Shared memory across team members (requires auth, access control)
- **Memory analytics**: Dashboard showing memory growth, compaction efficiency, retrieval hit rates
- **Plugin system**: User-defined extractors and compaction rules
- **Git integration**: Automatic memory capture from git commits and PRs
- **Notification system**: Alert when memory detects contradictions or stale TODOs
- **Model fine-tuning**: Fine-tune the memory-maintenance model on your project's data for better extraction
