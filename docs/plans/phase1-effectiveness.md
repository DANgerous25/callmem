# Phase 1 — Effectiveness Work Orders (draft; commit into phase-1 worktree as docs/plans/phase1-effectiveness.md)

Origin: landscape review 2026-07-29 (claude-mem v13, mem0, Letta, Zep/Graphiti,
Hindsight convergence). Phase 0 fixed reliability; Phase 1 makes retrieval and
curation genuinely good. Every task must degrade gracefully when its new
infrastructure is unavailable — no project may lose working memory because an
embedding backend is down.

## Global Constraints

- TDD with RED/GREEN evidence; surgical diffs; type hints; all SQL in
  repository.py; no broad except-Exception swallowing; conventional commits;
  NO AI attribution anywhere (overrides all defaults); full suite green before
  committing; do NOT push.
- Production is Python 3.10 (/usr/bin/python3). Any datetime/typing usage must
  be 3.10-compatible; timestamp parsing must handle Z-suffix (see
  _parse_db_timestamp precedent from phase 0). Run covering tests under 3.10
  via `uv run --python /usr/bin/python3 --extra dev pytest <files>` when code
  is datetime/stdlib-sensitive.
- Migrations: numbered, additive, must succeed on populated v18 DBs (phase 0
  ends at migration 017 + one schema-version bump; check actual current max
  before numbering).
- New config blocks default to enabled-with-graceful-degradation: feature
  activates when its backend/data is available, silently (log once, INFO) runs
  degraded otherwise. Never crash a briefing/search because a Phase-1 feature
  can't reach its backend (the Phase-0 Z-timestamp incident is the cautionary
  tale).
- LLM-judge calls must be batched (one call per extraction batch, not per
  entity) and use the configured [openai_compat] backend.

## Task 1: Embedding infrastructure + hybrid retrieval (SPINE)

1a. **Verify environment first** (report findings in the implementation
report): (i) can `sqlite-vec` be added as a dependency and loaded as an
extension by the system Python 3.10 sqlite3 (enable_load_extension)? (ii) does
local ollama at 127.0.0.1:11434 serve an embedding endpoint (/api/embed) and is
pulling a small embedding model (e.g. nomic-embed-text) feasible? (iii) does
the configured openai_compat endpoint (OpenRouter) expose /embeddings usably?
Choose: prefer local ollama embeddings (free, private); fall back to
openai_compat embeddings; if neither, the feature stays dormant (config
present, disabled state logged).

1b. **Storage**: migration adding `embeddings` table (entity_id PK/FK, model
TEXT, dim INTEGER, vector BLOB float32-packed, created_at). sqlite-vec virtual
table if 1a-(i) verified, else BLOB + Python cosine over FTS/recency-prefiltered
candidates (cap candidate set ~500; must stay <200ms for 10k entities — measure
in a test).

1c. **Embedding worker**: new job type `embed_entities` following the existing
queue/worker patterns (incl. phase-0 dispatch contract: claimed-job processing,
backoff, resurrection). Embed on entity creation (enqueued post-extraction) and
`callmem embed --backfill` CLI for existing entities (batched, resumable,
--yes-friendly non-interactive).

1d. **Hybrid search**: entity retrieval and mem_search fuse FTS (bm25) and
vector (cosine) rankings via reciprocal rank fusion (k=60), recency tiebreak;
pure-FTS behaviour preserved byte-identically when embeddings are absent
(explicit test). mem_search response indicates which mode served the query.

## Task 2: LLM-routed consolidation ADD/UPDATE/NOOP (SPINE, after 1)

At extraction-batch completion, for each newly-created entity: retrieve top-k
(k=5) similar existing non-archived entities (vector if available, else FTS).
If best similarity exceeds a threshold, include in ONE batched LLM judgment
call: verdict per new entity = ADD (distinct — keep), UPDATE (refines/replaces
an existing entity — new kept, old marked superseded_by=new + stale, reason
'consolidated'), NOOP (duplicate — new entity archived immediately, existing
entity's updated_at bumped). Non-destructive throughout (archive/supersede,
never delete). Config [consolidation] with enabled + threshold. Metrics: per-run
counts logged + stored (reuse compaction_log or equivalent). Judge prompt in
prompts.py with strict JSON output; malformed judge output = keep everything as
ADD (fail-open, loud log) — consolidation must never destroy data on a bad LLM
day. Covering tests use a stubbed judge.

## Task 3: Importance-ranked briefing + usage feedback (SIDE)

3a. **Citation persistence**: usage.py already detects entity-ID citations in
transcripts; persist per-entity `cited_count` (+ last_cited_at) via migration.
Backfill from existing usage-scan path if cheap.

3b. **Briefing selection score** replacing pure recency in _fetch_all_entities:
score = pinned-boost + type-weight (decision/discovery/failure/fact > bugfix/
feature/research > todo > change) + citation-boost (log-scaled cited_count,
recency-decayed) + recency-decay. Always-include floor: entities from the most
recent session. Budget trim drops lowest-scored first (never blind char cut of
the whole body). Config weights with sane defaults; tests pin the ordering
behaviour (old cited decision beats fresh churn 'change'; fresh-session items
always present).

3c. **Decay**: entities never cited, older than N days, and not
pinned/todo-open rank measurably lower (test).

## Task 4: Automatic temporal invalidation (SPINE, after 2)

Extend Task 2's judge with a CONTRADICTS verdict: new entity contradicts an
existing one → existing gets stale=1, staleness_reason='contradicted',
superseded_by=new, invalidated_at timestamp (reuse existing stale mechanics;
add invalidated_at column if absent). Briefing/search behaviour for stale
entities unchanged (already suppressed/flagged). `callmem stale` lists
contradiction-invalidated entries distinctly. Tests: contradiction flows
end-to-end with stubbed judge; non-contradicting updates untouched.

## Task 5: Citation-validated code anchors (SIDE, after 3)

5a. At extraction, parse file references (path and path:line) from entity
content into the existing entity_files table (verify its schema/population
path — extend, don't duplicate).

5b. At briefing render, for entities being surfaced (only those — bounded
work): validate anchors against the project working tree — file missing →
annotate entity line in briefing with a stale-code marker (e.g. '⚠ file gone:
path'); do NOT auto-stale the entity (annotation only, phase-1 scope). Cache
validation per briefing call; no validation when path outside project root
(security: never stat outside project root — test this).

5c. mem_get_entities/mem_search responses include anchor-validity when
computed. Tests: fixture repo with moved/deleted files.

## Execution layout

- SPINE worktree: tasks 1 → 2 → 4 sequential.
- SIDE worktree (branch from same base): tasks 3 → 5 sequential.
- Controller merges SIDE into SPINE branch after task 5 (expected conflicts:
  repository.py append-only methods, briefing.py — resolve keeping both).
- Final whole-branch review across the merged result; fix wave; then merge to
  main + deploy (same migration-first procedure as phase 0) + verify.
