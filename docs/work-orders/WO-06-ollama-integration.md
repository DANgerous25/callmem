# WO-06: Ollama Integration and Entity Extraction

## Objective

Implement the Ollama client and the entity extraction worker that processes raw events and extracts structured entities (decisions, TODOs, facts, failures, discoveries).

## Files to create

- `src/callmem/core/ollama.py` — Ollama HTTP client
- `src/callmem/core/extraction.py` — Entity extraction worker
- `src/callmem/core/prompts.py` — Prompt templates for extraction, summarization, etc.
- `src/callmem/core/queue.py` — Simple SQLite-backed job queue
- `tests/unit/test_ollama.py`
- `tests/unit/test_extraction.py`
- `tests/unit/test_queue.py`

## Files to modify

- `src/callmem/core/engine.py` — Queue extraction jobs after ingest
- `src/callmem/core/migrations/001_initial.sql` — Add `jobs` table if not already present
- `pyproject.toml` — Add `httpx` dependency

## Constraints

- Ollama client uses `httpx` for HTTP (sync for now)
- Use Ollama's `/api/generate` or `/api/chat` endpoint (not embeddings)
- Extraction prompt must request JSON output
- Parse JSON responses defensively — Ollama output can be malformed
- If Ollama is unavailable, queue jobs and retry later (don't block ingest)
- The job queue is SQLite-backed (a `jobs` table) — no Redis, no Celery
- Extraction runs in batches: process N unprocessed events per run
- Each extracted entity links back to its source event via `source_event_id`

## Job queue schema

```sql
CREATE TABLE jobs (
    id          TEXT PRIMARY KEY,
    type        TEXT NOT NULL,     -- extract_entities, generate_summary, compact
    payload     TEXT NOT NULL,     -- JSON: {event_ids: [...], session_id: "..."}
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending, running, completed, failed
    attempts    INTEGER DEFAULT 0,
    max_attempts INTEGER DEFAULT 3,
    created_at  TEXT NOT NULL,
    started_at  TEXT,
    completed_at TEXT,
    error       TEXT
);
CREATE INDEX idx_jobs_status ON jobs(status, created_at);
```

## Extraction prompt template

```python
EXTRACTION_PROMPT = """Analyze this coding session exchange and extract structured information.

Events:
{events_text}

Extract the following (only include items that are clearly present — do not invent):

1. **Decisions**: What was decided and why
2. **TODOs**: Tasks mentioned that need to be done (with priority if stated)
3. **Facts**: Durable project knowledge (e.g., "the API uses cursor-based pagination")
4. **Failures**: What went wrong, error messages, whether resolved
5. **Discoveries**: Notable insights or learnings

Respond in this exact JSON format:
{{
  "decisions": [{{ "title": "...", "content": "..." }}],
  "todos": [{{ "title": "...", "content": "...", "priority": "high|medium|low", "status": "open" }}],
  "facts": [{{ "title": "...", "content": "..." }}],
  "failures": [{{ "title": "...", "content": "...", "status": "unresolved|resolved" }}],
  "discoveries": [{{ "title": "...", "content": "..." }}]
}}

If a category has no items, use an empty array. Do not include explanatory text outside the JSON."""
```

## Acceptance criteria

1. `OllamaClient` can send a prompt and receive a response
2. `OllamaClient` handles connection errors gracefully (returns `None` or raises typed exception)
3. `OllamaClient` respects timeout configuration
4. Extraction worker processes a batch of events and produces entities
5. Extracted entities are stored in the `entities` table with correct `source_event_id`
6. Job queue enqueues, dequeues, marks complete, and retries failed jobs
7. If Ollama is down, extraction jobs are queued and processed when available
8. Malformed JSON responses from Ollama are handled (logged, job retried)
9. `pytest tests/unit/test_extraction.py tests/unit/test_queue.py` passes

## Suggested tests

```python
# Ollama client (mock HTTP)
def test_ollama_generate(mock_httpx):
    mock_httpx.post("http://localhost:11434/api/generate", json={"response": '{"decisions": []}'})
    client = OllamaClient(endpoint="http://localhost:11434", model="qwen3:8b")
    result = client.generate("test prompt")
    assert result is not None

def test_ollama_handles_timeout(mock_httpx):
    mock_httpx.post("http://localhost:11434/api/generate", side_effect=httpx.TimeoutException)
    client = OllamaClient(endpoint="http://localhost:11434", model="qwen3:8b")
    result = client.generate("test prompt")
    assert result is None

# Extraction
def test_extraction_produces_entities(engine, mock_ollama):
    mock_ollama.set_response('{"decisions": [{"title": "Use Redis", "content": "Chose Redis for caching"}], "todos": [], "facts": [], "failures": [], "discoveries": []}')
    engine.start_session()
    engine.ingest_one("response", "I recommend using Redis for caching because...")
    extractor = EntityExtractor(engine, mock_ollama)
    entities = extractor.process_pending()
    assert len(entities) == 1
    assert entities[0].type == "decision"
    assert entities[0].title == "Use Redis"

# Job queue
def test_queue_enqueue_dequeue(db):
    queue = JobQueue(db)
    queue.enqueue("extract_entities", {"event_ids": ["abc"]})
    job = queue.dequeue("extract_entities")
    assert job is not None
    assert job.status == "running"

def test_queue_retry_on_failure(db):
    queue = JobQueue(db)
    queue.enqueue("extract_entities", {"event_ids": ["abc"]})
    job = queue.dequeue("extract_entities")
    queue.fail(job.id, "Ollama timeout")
    job = queue.dequeue("extract_entities")  # Should be available again
    assert job.attempts == 2
```
