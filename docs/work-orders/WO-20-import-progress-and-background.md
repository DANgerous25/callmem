# WO-20: Import Progress Display and Background Execution

## Priority: P0

## Objective

The session import during setup and via CLI takes significant time on large histories. Improve the experience with progress indicators and an option to run the import in the background so users can start working immediately.

---

## 1. Progress Display During Import

### Problem
The import process reads sessions from OpenCode's SQLite DB, runs sensitive data scanning (LLM call per event batch), and queues extraction jobs. For large histories (50+ sessions, hundreds of events), this can take several minutes with no feedback.

### Required Behaviour

Show a running progress indicator during import:

```
Importing OpenCode sessions...
  Discovering sessions... found 19 sessions (546 events)
  Importing: [████████████░░░░░░░░] 12/19 sessions (312 events)  
  Session 12: "Fix auth redirect" — 28 events
```

After completion:
```
Import complete:
  Sessions: 19 imported
  Events:   546 ingested
  Jobs:     55 extraction jobs queued
  Time:     2m 34s

Extraction will continue in the background via the worker.
```

### Implementation

#### CLI import command (`src/llm_mem/cli.py`)

- Use `click.progressbar()` or a simple counter that prints progress per session
- Show: session count, event count, current session title
- Print summary stats at the end (sessions, events, jobs queued, elapsed time)

#### Setup wizard (`scripts/setup.py`)

- Same progress display in `_offer_session_import()`
- After import, print a message explaining that extraction runs in the background

### Files to modify
- `src/llm_mem/cli.py` — add progress bar to `import` command
- `src/llm_mem/adapters/opencode_import.py` — yield progress callbacks during import (currently returns all at once)
- `scripts/setup.py` — progress display in `_offer_session_import()`

---

## 2. Background Import Option

### Problem
Users can't start working until the import finishes. The import and extraction are independent of the SSE adapter and MCP server, so there's no reason to block.

### Required Behaviour

#### In setup wizard
After the user confirms import, offer two choices:

```
Import 19 sessions (estimated ~3 minutes)?
  [1] Import now (wait for completion)
  [2] Import in background (start working immediately)
```

If background: fork the import to a subprocess and continue with the rest of setup (systemd install, etc.). Print:

```
Import running in background (PID 12345).
Check progress: llm-mem import --status
Extraction will begin automatically once events are ingested.
You can open OpenCode now — new memories will appear as they're processed.
```

#### In CLI
Add `--background` flag:

```bash
llm-mem import --source opencode --opencode-db ~/.local/share/opencode/opencode.db --background
```

This forks the import to a background process and returns immediately.

#### Status check
Add `--status` flag to show import progress:

```bash
llm-mem import --status
```

Output:
```
Import in progress:
  Sessions: 12/19 imported
  Events:   312/546 ingested
  Jobs:     31 extraction jobs queued, 8 completed
```

Or if no import is running:
```
No import in progress.
Last import: 19 sessions, 546 events (completed 2m ago)
```

### Implementation

#### Background process

Use a simple approach:
1. Write a progress file to `.llm-mem/import_progress.json`:
   ```json
   {
     "pid": 12345,
     "started_at": "2026-04-17T13:50:00",
     "total_sessions": 19,
     "imported_sessions": 12,
     "total_events": 546,
     "imported_events": 312,
     "status": "running"
   }
   ```
2. The import process updates this file as it progresses
3. `--status` reads the file and displays progress
4. On completion, set `status: "completed"` with a timestamp

#### Forking

Use `subprocess.Popen` with the same CLI command + an internal `--_foreground` flag (so the background process doesn't try to fork again).

Or use `multiprocessing.Process(daemon=True)` since we're already in Python.

### Safety

The import is safe to run concurrently with:
- **OpenCode SSE adapter** — adapter writes new events, import writes historical events. Different session IDs, no conflicts.
- **Extraction worker** — worker reads from the job queue. Import adds jobs to the queue. SQLite handles concurrent reads/writes with WAL mode.
- **Web UI** — read-only queries, no conflicts.
- **MCP server** — read-only queries, no conflicts.

The only constraint: don't run two imports simultaneously. Use a lockfile (`.llm-mem/import.lock`) to prevent this.

### Files to modify
- `src/llm_mem/cli.py` — add `--background` and `--status` flags
- `src/llm_mem/adapters/opencode_import.py` — write progress to `.llm-mem/import_progress.json`
- `scripts/setup.py` — offer background import option

### Files to create
- Progress tracking logic (can be inline in `opencode_import.py` or a small helper)

---

## Acceptance Criteria

### Progress Display
1. [ ] CLI import shows per-session progress (session N/M, event count, session title)
2. [ ] CLI import shows summary on completion (sessions, events, jobs queued, elapsed time)
3. [ ] Setup wizard shows same progress during interactive import
4. [ ] Progress updates in real-time (not buffered until end)

### Background Import
5. [ ] `llm-mem import --background` forks import and returns immediately
6. [ ] Setup wizard offers "import now" vs "import in background" choice
7. [ ] Background import writes progress to `.llm-mem/import_progress.json`
8. [ ] `llm-mem import --status` shows current progress or last import summary
9. [ ] Lockfile prevents concurrent imports
10. [ ] Import is safe to run while daemon, adapter, and OC are active

### General
11. [ ] All existing tests pass, new tests for progress tracking and background mode
12. [ ] `make lint` clean, `make test` all pass
13. [ ] Committed and pushed
