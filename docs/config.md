# Configuration

## Config loading order

Configuration is resolved in this order (later overrides earlier):

1. **Built-in defaults** — Sensible defaults baked into the code
2. **Global config** — `~/.config/llm-mem/config.toml`
3. **Project config** — `.llm-mem/config.toml` in the project root
4. **Environment variables** — `LLM_MEM_*` prefix
5. **CLI flags** — Command-line arguments

## Config file format

TOML. Chosen over JSON because it supports comments, and over YAML because it's unambiguous.

### Full example

```toml
# ~/.config/llm-mem/config.toml  (global)
# or .llm-mem/config.toml        (per-project)

[project]
name = "my-project"
root_path = "."                    # Resolved to absolute path at runtime

[database]
path = ".llm-mem/memory.db"       # Relative to project root
wal_mode = true                    # SQLite WAL mode for concurrent access
busy_timeout_ms = 5000

[ollama]
endpoint = "http://localhost:11434"
model = "qwen3:8b"                 # Model for summarization / extraction / compaction
timeout_s = 120                    # Per-request timeout
max_retries = 3
enabled = true                     # Set false to skip all background LLM work

[interactive]
# These describe the interactive coding model — llm-mem doesn't call it,
# but uses this info for token estimation and briefing sizing.
provider = "anthropic"             # anthropic, openai, ollama, google, etc.
model = "claude-sonnet-4-5-20250514"
context_window = 200000            # Tokens — used to size briefings appropriately

[session]
inactivity_timeout_m = 30          # Minutes of no events before session auto-ends
auto_start = true                  # Auto-create session on first event if none active
auto_briefing = true               # Automatically serve briefing on session start

[ingest]
capture_prompts = true
capture_responses = true
capture_tool_calls = true
capture_file_changes = true
max_event_size_chars = 50000       # Truncate events larger than this
dedup_window_s = 5                 # Ignore duplicate events within this window

[extraction]
enabled = true                     # Enable entity extraction from raw events
batch_size = 10                    # Process this many events per extraction run
delay_s = 5                        # Wait this long after last event before extracting
types = ["decision", "todo", "fact", "failure", "discovery"]

[summarization]
enabled = true
chunk_size = 20                    # Events per chunk summary
session_summary_on_end = true      # Generate session summary when session ends
cross_session_interval = 5         # Generate cross-session summary every N sessions

[compaction]
enabled = true
schedule = "on_session_end"        # on_session_end, hourly, daily, manual
thresholds = { raw_events_age_days = 1, summaries_age_days = 7, full_archive_age_days = 30 }
max_db_size_mb = 500               # Trigger aggressive compaction above this
protect_pinned = true              # Never compact pinned entities
protect_active_todos = true        # Never compact open TODOs

[briefing]
max_tokens = 2000                  # Token budget for startup briefing
include_todos = true
include_decisions = true
include_failures = true            # Only unresolved
include_facts = true               # Only pinned
include_last_session_summary = true
include_project_summary = true
recency_window_sessions = 3        # Include context from last N sessions

[retrieval]
default_limit = 20
max_limit = 100
strategies = ["structured", "fts5", "recency"]  # Order = priority
fts5_weight = 1.0
recency_weight = 0.8
structured_weight = 1.2
# v2: add "semantic" to strategies list
# semantic_weight = 0.9

[redaction]
enabled = true
pattern_scan = true
llm_scan = true
llm_scan_confidence = 0.7
detect_secrets = true
detect_credentials = true
detect_pii = true
detect_financial = true
detect_infrastructure = false      # Off by default — noisy for dev work
entropy_enabled = true
entropy_threshold = 4.5
entropy_min_length = 20
allowlist = ["test@example.com", "127.0.0.1", "localhost"]

[vault]
mode = "auto"                      # "auto" or "passphrase"
# passphrase read from LLM_MEM_VAULT_PASSPHRASE env var when mode = "passphrase"

[embeddings]
# v2 configuration — ignored in v1
enabled = false
backend = "local"                  # "local" (sentence-transformers) or "api" (openai)
model = "all-MiniLM-L6-v2"        # For local backend
api_endpoint = ""                  # For API backend
api_key_env = "EMBEDDING_API_KEY"  # Env var containing API key
dimensions = 384
batch_size = 32

[ui]
host = "127.0.0.1"
port = 9090
open_browser = true                # Auto-open browser on `llm-mem ui`

[mcp]
transport = "stdio"                # stdio or sse
sse_host = "127.0.0.1"
sse_port = 9091

[logging]
level = "INFO"                     # DEBUG, INFO, WARNING, ERROR
file = ".llm-mem/llm-mem.log"     # Relative to project root
max_size_mb = 10
backup_count = 3
```

## Environment variables

Every config key maps to an environment variable with `LLM_MEM_` prefix and `__` as separator:

| Config key | Environment variable |
|---|---|
| `ollama.model` | `LLM_MEM_OLLAMA__MODEL` |
| `ollama.endpoint` | `LLM_MEM_OLLAMA__ENDPOINT` |
| `database.path` | `LLM_MEM_DATABASE__PATH` |
| `briefing.max_tokens` | `LLM_MEM_BRIEFING__MAX_TOKENS` |
| `ui.port` | `LLM_MEM_UI__PORT` |
| `mcp.transport` | `LLM_MEM_MCP__TRANSPORT` |

## CLI flags

```bash
# Override project root
llm-mem serve --project /path/to/project

# Override Ollama model
llm-mem serve --ollama-model qwen3:14b

# Override database path
llm-mem serve --db /custom/path/memory.db

# Override UI port
llm-mem ui --port 8080

# Override MCP transport
llm-mem serve --transport sse --sse-port 9091

# Disable background workers (ingest-only mode)
llm-mem serve --no-workers

# Verbose logging
llm-mem serve -v  # INFO
llm-mem serve -vv # DEBUG
```

## Safe defaults

The default configuration is designed to be safe and useful without any user configuration:

| Setting | Default | Why |
|---|---|---|
| Database path | `.llm-mem/memory.db` | In the project directory, git-ignorable |
| Ollama model | `qwen3:8b` | Good balance of quality and speed on modest hardware |
| Compaction | Enabled, on session end | Prevents unbounded growth |
| Briefing budget | 2000 tokens | Leaves room for coding context |
| FTS5 | Enabled | Works with zero configuration |
| Embeddings | Disabled | Requires explicit opt-in |
| UI host | `127.0.0.1` | Not exposed to network |
| MCP transport | stdio | Standard for OpenCode |
| Logging | INFO to file | Not noisy, but diagnosable |

## `.gitignore` recommendation

Add to your project's `.gitignore`:

```gitignore
# llm-mem
.llm-mem/
```

The entire `.llm-mem/` directory (database, logs, config) is local to each developer's machine. It should not be committed.

## Config validation

On startup, llm-mem validates:
1. Ollama endpoint is reachable (warn if not — don't fail)
2. Database path is writable
3. All config values are within acceptable ranges
4. Unknown config keys produce warnings (not errors) for forward compatibility
