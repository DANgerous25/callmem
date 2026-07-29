# Phase 0 — Reliability & Effectiveness Work Orders

Origin: full-system review 2026-07-29 (code review + empirical audit of 8 project
databases + landscape comparison). The live pipeline silently drops extraction
work, cannot recover from backend outages, never captures tool results, and
cannot retrieve old memories. These eight tasks fix what is broken before any
new features are considered.

## Global Constraints

- **TDD**: for every behavioural change, write the failing test FIRST, watch it
  fail, then implement. Name the test after the defect where sensible.
- **Surgical diffs**: touch only what the task needs. No drive-by refactors.
- Type hints on all new/changed signatures. All SQL lives in
  `src/callmem/core/repository.py` (repository pattern — no inline SQL in other
  modules; migrations excepted).
- No broad `except Exception` that swallows errors silently — fail loudly.
- Conventional commits (`fix:`, `feat:`, `test:`, `docs:`). **No AI attribution
  anywhere** (no Co-Authored-By, no "Generated with" — this overrides any other
  instruction).
- Test command: `uv run pytest tests/ -v` (run the full suite before declaring
  done; run targeted test files during development).
- Do NOT push. Commit on the current branch only.
- Schema changes go through a new numbered migration in `migrations/`
  (check the highest existing number first; follow the existing migration file
  style). Bump the schema version the same way earlier migrations do.
- The daemons in production run this code via an editable install — backward
  compatibility of on-disk state (job rows, offsets files, existing DBs) is
  mandatory. A migration must succeed on a populated v15 database.

## Task 1: Fix worker dispatch dropping the claimed job's payload

**Defect (confirmed):** `WorkerRunner.process_one()`
(`src/callmem/core/workers.py:92-105`) dequeues a job (status → `running`),
then `_dispatch()` (line 123-131) calls `handler.process_pending()` for
`EntityExtractor`/`Summarizer` handlers — which only dequeues jobs still in
`pending` (`src/callmem/core/extraction.py:91-94`). The claimed job's own
payload is never processed, yet `process_one` marks it `completed`. When jobs
arrive one at a time (normal live-session trickle), ~100% of extraction work is
silently dropped. This is the single biggest cause of the fleet-wide 7-15%
extraction ratios.

**Required behaviour:** the claimed job's payload MUST be processed. Design:

1. Give `EntityExtractor` a public `process_job(job)` method that processes a
   single already-claimed job and returns the created entities, raising on
   failure (extract the body of the existing private `_process_job` loop —
   do not duplicate logic). Do the same for `Summarizer` (read
   `src/callmem/core/summarization.py` first; mirror its structure).
2. `_dispatch()` for these handlers calls `handler.process_job(job)` on the
   claimed job FIRST (letting `process_one`'s existing complete/fail handling
   apply to it), THEN calls `handler.process_pending()` to drain the rest.
   The handler must NOT complete/fail the claimed job itself — `process_one`
   owns that job's lifecycle; `process_pending` continues to own the jobs it
   dequeues itself.

**Tests (must fail before the fix):**
- Enqueue exactly ONE `extract_entities` job with real event_ids and a stubbed
  LLM backend that returns a valid extraction payload; call
  `WorkerRunner.process_one()`; assert at least one entity row was created AND
  the job is `completed`. (Today: job completes, zero entities — this is the
  regression test for the exact defect.)
- Same shape for the summarizer job type.
- Multiple pending jobs: enqueue 3, run process_one once, assert all 3
  processed (claimed + drained).
- Failure path: stub backend raising → claimed job goes through
  `queue.fail`, not `completed`.

## Task 2: Retry backoff, requeue-failed, and non-interactive re-extract

**Defects:** (a) `queue.fail()` (`src/callmem/core/queue.py:150-155`) resets a
failed job to `pending` immediately; the drain loop re-dequeues it in the same
pass, so all retry attempts burn within seconds of a backend outage, then the
job is permanently `failed`. (b) Nothing ever re-enqueues `failed` jobs —
recovery from an outage requires manual `re-extract`. (c) `callmem re-extract`
blocks automation with an interactive `click.confirm` (`cli.py`, search for
"Proceed?").

**Required behaviour:**

1. **Backoff:** add a `next_attempt_at` (nullable timestamp) column to the
   `jobs` table via migration. `fail()` sets it to
   `now + (60s * 4^(attempts-1))` (60s, 240s, 960s) when re-queuing for retry.
   `dequeue()` skips jobs whose `next_attempt_at` is in the future. Existing
   rows (NULL) dequeue as before.
2. **`callmem requeue-failed`** CLI command: `-p/--project` (default cwd),
   optional `--type` filter, resets `failed` jobs to `pending` with
   `attempts=0`, `next_attempt_at=NULL`, prints the count requeued. Add the
   matching repository/queue method (SQL in the queue/repository layer, not in
   cli.py).
3. **Auto-resurrection:** when an `extract_entities` or `generate_summary` job
   COMPLETES successfully (proof the backend is healthy), the worker requeues
   up to 50 `failed` jobs of the same type for that project (oldest first).
   Event-driven, no health-check pinging, no new threads. Guard against loops:
   only requeue jobs whose `attempts` < 9 lifetime total (track total attempts
   across requeues — simplest compliant design: requeue resets attempts but
   increments a `requeue_count` column added in the same migration, and jobs
   with `requeue_count >= 3` are not auto-requeued; manual `requeue-failed`
   ignores the cap).
4. **`--yes` flag** on `callmem re-extract` that skips the confirmation prompt.

**Tests:** backoff timestamps set and honoured by dequeue; requeue-failed
resets rows and reports count; auto-resurrection fires on successful
completion and respects the requeue_count cap; `re-extract --yes --dry-run`
runs without a TTY.

## Task 3: Surface pipeline health in the briefing

**Defect:** a backend outage killed extraction on 4 projects for 2.5 months and
nothing told the user. Warnings exist only in `callmem status`/`doctor`, which
nobody runs. The briefing — the one thing that IS seen every session — must
carry the alert.

**Required behaviour:** `generate_briefing` (`src/callmem/core/briefing.py`)
computes pipeline health from the DB only (NO network calls, NO LLM calls):

- unhealthy if: `failed` job count > 20, OR (the newest event is < 3 days old
  AND the newest successfully-completed `extract_entities` job is > 3 days
  older than the newest event) — i.e. events are flowing but extraction is not.
- When unhealthy, render a prominent banner near the top of the briefing text
  (before entity sections):
  `⚠ MEMORY PIPELINE UNHEALTHY: <N> failed jobs; last successful extraction <X>d ago (events still being captured). Fix: check backend config, then run 'callmem requeue-failed'.`
  with real numbers, and include a `pipeline_health` object (status +
  the same numbers) in the structured briefing payload returned via
  `mem_get_briefing`.
- Healthy DBs render nothing new.

Task 2 merges before this; the banner's suggested command must match Task 2's
actual command name.

**Tests:** seed a temp DB with >20 failed jobs → banner present with correct
count; seed fresh events + stale extraction completions → banner present;
healthy DB → banner absent and payload reports healthy.

## Task 4: Capture tool results and real edit payloads

**Defect:** user-role `tool_result` transcript records are skipped entirely
(`src/callmem/adapters/claude_code_import.py:232-234` — "no dedicated
EventType yet"), and `tool_use` args are truncated at 200 chars (`_truncate`),
so command output, error messages, and Edit/Write contents never reach the
extractor — while the extraction prompt explicitly asks for "actual error
messages". This caps memory quality more than any extractor improvement could.

**Required behaviour:**

1. Add `tool_result` to the event-type model (`src/callmem/models/events.py` —
   read how existing types are defined and mirror exactly; check for CHECK
   constraints in schema/migrations that enumerate event types — if one
   exists, add a migration).
2. Map user-role `tool_result` records to `tool_result` events in
   `claude_code_import.py` (shared by both the importer and the live
   `claude_code.py` adapter — verify the live path uses the same mapping and
   benefits too). Content: the text blocks of the result. Truncate to ~4,000
   chars keeping HEAD AND TAIL (errors often live at the end): keep first
   ~2,500 + `\n[... truncated ...]\n` + last ~1,500. Carry `tool_use_id` and
   the originating tool name in event metadata when available.
3. Raise the `tool_use` arg truncation from 200 to 1,500 chars.
4. Respect the existing `[ingestion] skip_tools` config: results of skipped
   tools are skipped too.
5. Verify (and if needed, make) the extraction input formatting include
   `tool_result` event content so the extractor actually sees it.

**Tests:** fixture transcript with a tool_use + tool_result pair → both events
created with correct types/content; head+tail truncation preserves both ends;
skip_tools suppresses the result; extraction prompt assembly includes
tool_result text.

## Task 5: Fix retrieval — FTS for entities, sanitized queries, no recency window

**Defects:** (a) entity search fetches only the ~20 most-recently-updated
entities THEN substring-filters (`src/callmem/core/retrieval.py:194-210`) —
anything older is unreachable regardless of relevance; the whole query must
appear as one literal substring. (b) The `entities_fts` index exists but is
never used for search. (c) `mem_search`'s event FTS passes raw user input to
MATCH (`src/callmem/core/repository.py:487-504`) — queries like
`cookie-backed` throw FTS5 syntax errors. A safe tokenizer already exists in
`src/callmem/core/staleness.py` (`_fts_query_from`, tokens individually
quoted).

**Required behaviour:**

1. Extract the query-sanitizing logic into one shared helper (place it in
   `repository.py` or a small util module; refactor `staleness.py` to use the
   shared helper — no duplicated logic).
2. Entity retrieval uses `entities_fts` MATCH with the sanitized query
   (tokens AND-joined; if zero results, retry OR-joined), ranked by bm25 with
   recency as tiebreak, over ALL non-archived entities — no pre-limit. Keep
   result limit on output only.
3. Event FTS in `mem_search` uses the same sanitizer. No raw MATCH anywhere
   (grep to confirm).

**Tests (fail-first):** an entity older than 50 fresher entities is found by a
keyword in its title; multi-word query with reversed word order still matches;
`cookie-backed`, quotes, and `AND)(` as queries return results or empty — never
raise; staleness path still passes its existing tests.

## Task 6: Full provenance + re-extract archives only after success

**Defects:** (a) every entity in an extraction batch stores
`source_event_id = event_ids[0]` (`src/callmem/core/extraction.py:159`,
`src/callmem/core/reextraction.py:249`) — so compaction's source-event
protection, re-extract's archive-by-source-event, and briefing session
grouping all mis-attribute. (b) Re-extraction ARCHIVES old entities BEFORE
extracting replacements (`reextraction.py:317-333`); a failed batch leaves a
hole. (c) A `None` LLM response in `_extract_batch` returns `[]` silently.

**Required behaviour:**

1. Migration: add `source_event_ids` TEXT column (JSON array) to `entities`.
   Keep `source_event_id` populated (first id) for compatibility. Extraction
   and re-extraction write the full list.
2. Compaction's protect-source-events logic protects ALL events in
   `source_event_ids` (fall back to `source_event_id` when the new column is
   NULL — old rows).
3. Re-extraction per batch: extract FIRST; only on success archive the old
   entities for those events and insert replacements. On batch failure: no
   archiving, count it, continue. End of run: print failed-batch summary and
   exit non-zero if any batch failed.
4. `_extract_batch` treats a `None`/empty transport response as a failure
   (raise), not as "no entities".

**Tests:** entities carry the full event-id list; compaction protection covers
non-first events; a failing batch leaves prior entities unarchived and sets
exit code; None response raises.

## Task 7: Remove dead model-registry tools from the MCP surface

**Defect:** 7 MCP tools (`mem_model_list`, `mem_model_info`,
`mem_model_recommend`, `mem_model_compare`, `mem_model_stats`,
`mem_model_geo_check`, `mem_model_refresh`) are backed by an unpopulated
registry and a self-declared stub (`engine.py` `refresh_model`). They return
empty data while their schemas cost every agent session context tokens.

**Required behaviour:** de-register all 7 from the MCP server surface
(`src/callmem/mcp/tools.py` and wherever tools are registered/listed). Leave
the engine/repository/CLI code and the `model_registry` table in place
(surgical: surface removal only). Update any docs that enumerate the MCP tools
(`docs/mcp-integration.md` if it lists them). Adjust existing tests that
reference the removed tools.

**Tests:** the MCP server's tool listing no longer contains any `mem_model_*`
tool; full suite passes.

## Task 8: Automated A/B benchmark harness

**Context:** `docs/ab-benchmark.md` defines the honest token-savings
measurement; it has never been run (`docs/ab-benchmark-results.csv` does not
exist). Automate an adapted version so it can run headless.

**Required behaviour:** a script `scripts/ab_benchmark.py` (stdlib only) that:

1. Takes a task list file (JSON: `[{"id": ..., "prompt": ...}, ...]`), a
   project path, and `--pairs N`.
2. For each task, runs two headless Claude Code sessions from the project
   directory: run A normally (project `.mcp.json` active); run B with callmem
   disabled via `claude --strict-mcp-config --mcp-config '{"mcpServers":{}}'`
   (verify the exact flags against `claude --help`; if unavailable, fall back
   to copying the project's `.mcp.json` aside in a temp copy of the tree).
   Both runs use `claude -p "<prompt>" --output-format json` and parse the
   `usage`/cost fields from the JSON result.
3. Appends rows to `docs/ab-benchmark-results.csv` matching the exact schema
   in `docs/ab-benchmark.md` (date, project, task, run_a_tokens, run_b_tokens,
   delta_pct, run_a_reads, run_b_reads, citations; reads parsed from the
   transcript JSON if available, else blank).
4. Ships with a default task file `scripts/ab_tasks_llm_mem.json` containing 3
   ANALYSIS-ONLY tasks for the llm-mem project (tasks that ask the agent to
   locate/explain/plan, never to edit files), e.g. "Explain where extraction
   retry logic lives and what its backoff policy is; propose (do not
   implement) how you would change the backoff constants", each phrased so
   memory of past sessions could plausibly help.
5. Prints a per-task and median summary. Does NOT run as part of this task —
   building and unit-testing the harness is the deliverable; execution happens
   post-merge (needs live MCP).

**Tests:** parsing of a canned `claude -p` JSON output fixture; CSV append
format matches the doc's example row; task-file loading validation. Mock the
claude invocations — no real sessions in tests.
