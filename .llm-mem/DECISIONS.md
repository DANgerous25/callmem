# Design Decisions

Append-only log. Record decisions as they are made. Never delete entries — mark superseded ones with ~~strikethrough~~.

---

### 001 — SQLite + FTS5, not vector DB (2026-04-16)
**Decision:** Use SQLite with FTS5 full-text search as the primary storage and retrieval engine. Vector embeddings are optional phase 2.
**Why:** Most coding memory queries are structured ("what decisions about auth?", "open TODOs") or keyword-based. SQLite is zero-dependency, single-file, trivially backed up. FTS5 with BM25 ranking handles keyword search well. Vector search adds complexity and a mandatory embedding model before v1 delivers any value.

### 002 — ULID IDs, not UUID or autoincrement (2026-04-16)
**Decision:** All primary keys are ULID strings.
**Why:** Time-sortable (ORDER BY id is chronological), 26-char text (readable), no collisions across projects. Autoincrement breaks cross-project portability. UUIDs are not time-sortable.

### 003 — Repository pattern for SQL isolation (2026-04-16)
**Decision:** All SQL lives in `repository.py`. Engine and handlers never write SQL directly.
**Why:** Keeps the engine testable without a database. Makes it possible to swap storage later (unlikely but free insurance). Prevents SQL from scattering across the codebase.

### 004 — SQLite-backed job queue, not Redis/Celery (2026-04-16)
**Decision:** Background jobs use a `jobs` table in the same SQLite database.
**Why:** One fewer dependency. The workload is small (process a few events every few seconds). SQLite WAL mode handles concurrent reads/writes. If we ever need a real queue, the interface is clean enough to swap.

### 005 — Separate memory-maintenance LLM from interactive LLM (2026-04-16)
**Decision:** The Ollama model used for extraction/summarization/compaction is always separate from the coding model the user interacts with.
**Why:** Memory maintenance should not cost interactive tokens, add latency to the coding loop, or require the same provider. A small local model (qwen3:8b) is sufficient for extraction and summarization.

### 006 — TOML config, not JSON or YAML (2026-04-16)
**Decision:** Configuration uses TOML.
**Why:** Supports comments (JSON doesn't). Unambiguous syntax (YAML has the Norway problem and implicit typing). Python 3.11+ has `tomllib` in stdlib.

### 007 — FastAPI + htmx + Pico CSS for web UI (2026-04-16)
**Decision:** No JavaScript framework. Server-rendered HTML with htmx for interactivity, Pico CSS for styling.
**Why:** No build step, no Node.js dependency. FastAPI is already in the dependency tree for MCP SSE. htmx is a single 14KB file. Pico CSS is classless — semantic HTML looks decent with zero effort. Easy for a coding agent to modify.

### 008 — Bootstrap memory via flat files until llm-mem is self-hosting (2026-04-16)
**Decision:** Use `.llm-mem/SESSION.md`, `DECISIONS.md`, and `TODO.md` as a crude memory system during development.
**Why:** We're building a memory system but need memory while building it. Flat markdown files are readable by any agent, easily diffable, and zero-infrastructure.

### 009 — Two-layer inline sensitive data detection (2026-04-16)
**Decision:** All ingested content is scanned at ingest time (not async) using two layers: fast regex pattern matching first, then local Ollama LLM classification as a second pass. Detected secrets are Fernet-encrypted into a vault; memories store redacted placeholders.
**Why:** Pattern matching catches known formats (API keys, credit cards) but misses novel secrets. The local Ollama model catches fuzzy cases ("looks like a password") with zero privacy risk since it runs on the user's own machine. Running both inline at ingest means sensitive data never reaches the database in plain text — no window of exposure. Fernet symmetric encryption (key derived from user passphrase + salt) keeps the vault portable and auditable.

### 010 — /briefing command as interim startup briefing (2026-04-18)
**Decision:** Use the `/briefing` custom command as the primary way to view the llm-mem startup briefing in OpenCode, rather than auto-displaying it via the session.created plugin hook.
**Why:** An auto-briefing plugin (.opencode/plugins/auto-briefing.js) was built to hook session.created and auto-inject a prompt via the SDK. However, OpenCode has a known bug (anomalyco/opencode#14808) where session.created events do not fire for plugins. Until that upstream issue is resolved, `/briefing` is the reliable workaround. The plugin remains installed so it will activate automatically when the bug is fixed — no changes needed on our side.

### 011 — Retain unused events.scan_status column (2026-04-18)
**Decision:** The `events.scan_status` column (added in migration 002) remains in the schema but is not used by application code. The engine stores scan_status inside the event `metadata` JSON dict instead. The column is retained for potential future use as a directly queryable/indexed field.
**Why:** Removing the column would require a new migration and carries risk of breaking existing databases. The column has no performance cost (NULL values occupy negligible space). If direct SQL querying of scan_status becomes useful (e.g., "find all unscanned events"), the column is ready. Documented in migration 002 header.
