# WO-09: Memory Compaction

## Objective

Implement the compaction worker that manages memory growth by archiving old events and ensuring summaries cover the archived content.

## Files to create

- `src/llm_mem/core/compaction.py` — Compaction worker with age-based policies
- `tests/unit/test_compaction.py`

## Files to modify

- `src/llm_mem/core/engine.py` — Trigger compaction on session end or schedule
- `src/llm_mem/core/queue.py` — Add `compact` job type

## Constraints

- Compaction NEVER deletes data — it sets `archived_at` timestamp
- Pinned entities are never archived
- Active TODOs (status=open) are never archived
- Compaction only archives events that have been covered by a summary
- Compaction runs as a background job (uses job queue)
- Compaction logs every run in `compaction_log` table

## Compaction policy

```python
@dataclass
class CompactionPolicy:
    raw_events_age_days: int = 1       # Archive raw events older than this (if summarized)
    summaries_age_days: int = 7        # Archive chunk summaries older than this (if session summary exists)
    full_archive_age_days: int = 30    # Archive everything except pinned, active TODOs, and cross-session summaries
    max_db_size_mb: int = 500          # Trigger aggressive compaction above this size
    protect_pinned: bool = True
    protect_active_todos: bool = True
```

## Compaction algorithm

```
1. Get current DB size and compaction policy
2. If DB size > max_db_size_mb, reduce age thresholds by 50%
3. For each age tier:
   a. Find events older than threshold that have been summarized
   b. Exclude pinned events and events linked to active TODOs
   c. Set archived_at = NOW on matching events
4. For summaries tier:
   a. Find chunk summaries older than threshold where a session summary exists
   b. Set archived_at = NOW
5. Log the compaction run
6. Return stats: events_archived, summaries_created (if any gap summaries needed), entities_merged
```

## Acceptance criteria

1. Compaction archives events older than the configured threshold
2. Pinned entities survive compaction
3. Active TODOs survive compaction
4. Events without a covering summary are NOT archived (even if old)
5. Compaction log records each run with accurate stats
6. Aggressive compaction kicks in above `max_db_size_mb`
7. Archived events are excluded from default search results
8. Archived events are still retrievable with `include_archived=True`
9. `pytest tests/unit/test_compaction.py` passes

## Suggested tests

```python
def test_old_events_archived(engine_with_old_data, compactor):
    stats = compactor.run()
    assert stats.events_archived > 0

def test_pinned_entities_survive(engine_with_old_data, compactor):
    # Pin an entity
    engine_with_old_data.set_pinned(entity_id, True)
    compactor.run()
    entity = engine_with_old_data.get_entity(entity_id)
    assert entity.archived_at is None

def test_active_todos_survive(engine_with_old_data, compactor):
    compactor.run()
    todos = engine_with_old_data.get_entities(type="todo", status="open")
    assert all(t.archived_at is None for t in todos)

def test_unsummarized_events_survive(engine, compactor):
    # Ingest events but don't summarize
    engine.ingest_one("prompt", "Important event")
    # Artificially age the event
    compactor.run()
    events = engine.get_events()
    assert len(events) > 0  # Not archived because no summary covers it

def test_compaction_log_created(engine_with_old_data, compactor, db):
    compactor.run()
    logs = db.execute("SELECT * FROM compaction_log").fetchall()
    assert len(logs) == 1

def test_archived_excluded_from_search(engine_with_old_data, compactor):
    compactor.run()
    results = engine_with_old_data.search("old event keyword")
    assert len(results) == 0  # Archived, not in default results
    results = engine_with_old_data.search("old event keyword", include_archived=True)
    assert len(results) > 0  # But retrievable with flag
```
