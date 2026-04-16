# WO-08: Summarization Workers

## Objective

Implement the summarization system: chunk-level summaries, session-level summaries, and cross-session summaries. These run in the background using Ollama.

## Files to create

- `src/llm_mem/core/summarization.py` — Summary generation at all levels
- `tests/unit/test_summarization.py`

## Files to modify

- `src/llm_mem/core/prompts.py` — Add summarization prompt templates
- `src/llm_mem/core/engine.py` — Trigger summarization on session end and on chunk thresholds
- `src/llm_mem/core/queue.py` — Add `generate_summary` job type handling

## Constraints

- Summarization uses the same job queue as extraction
- Chunk summaries are generated when `chunk_size` events accumulate without a summary
- Session summaries are generated when a session ends
- Cross-session summaries are generated every N sessions (configurable)
- Each summary records which events it covers (`event_range_start`, `event_range_end`)
- Summaries are stored in the `summaries` table and indexed by FTS5
- If Ollama is unavailable, summarization jobs queue and retry later

## Summary levels

| Level | Trigger | Input | Output |
|---|---|---|---|
| Chunk | Every `chunk_size` events (default: 20) | Raw event content | 1-2 paragraph summary |
| Session | Session end | All chunk summaries + remaining events | Structured session summary |
| Cross-session | Every N sessions (default: 5) | Session summaries | Project-level summary |

## Prompt templates

```python
CHUNK_SUMMARY_PROMPT = """Summarize this batch of coding session events into a concise paragraph.
Focus on: what was worked on, what was accomplished, any problems encountered.

Events:
{events_text}

Write a 2-3 sentence summary. Be specific about files, functions, and technical details."""

SESSION_SUMMARY_PROMPT = """Summarize this entire coding session.

Session chunks:
{chunks_text}

Remaining events not yet summarized:
{remaining_events_text}

Produce a structured summary:
1. **What was done**: Main accomplishments
2. **Key decisions**: Important choices made
3. **Issues**: Problems encountered or unresolved
4. **TODOs**: Tasks identified for future work

Be concise but specific."""

CROSS_SESSION_PROMPT = """Synthesize these session summaries into a project-level overview.

Sessions:
{sessions_text}

Produce a concise project status covering:
1. Current state of the project
2. Major decisions and their rationale
3. Active work streams
4. Known issues and technical debt

Keep it under {max_tokens} tokens."""
```

## Acceptance criteria

1. Chunk summaries are generated after `chunk_size` events
2. Session summaries are generated when a session ends
3. Cross-session summaries are generated at the configured interval
4. Summaries are stored with correct `event_range_start` / `event_range_end`
5. Summaries are FTS5-indexed and searchable
6. Token count is estimated and stored on each summary
7. If Ollama is unavailable, jobs queue without blocking the session
8. `pytest tests/unit/test_summarization.py` passes

## Suggested tests

```python
def test_chunk_summary_triggered(engine, mock_ollama):
    mock_ollama.set_response("Implemented pagination for the query endpoint.")
    engine.start_session()
    for i in range(20):  # chunk_size
        engine.ingest_one("prompt", f"Event {i}")
    summarizer = Summarizer(engine, mock_ollama)
    summaries = summarizer.process_pending()
    assert len(summaries) == 1
    assert summaries[0].level == "chunk"

def test_session_summary_on_end(engine, mock_ollama):
    mock_ollama.set_response("Session focused on adding cursor-based pagination.")
    session = engine.start_session()
    engine.ingest_one("prompt", "Add pagination")
    engine.ingest_one("response", "Done, using cursor approach")
    engine.end_session(session.id)
    summarizer = Summarizer(engine, mock_ollama)
    summaries = summarizer.process_pending()
    session_summaries = [s for s in summaries if s.level == "session"]
    assert len(session_summaries) == 1

def test_summary_stored_in_fts(engine, mock_ollama, db):
    mock_ollama.set_response("Implemented Redis caching layer.")
    # ... trigger summary generation ...
    rows = db.execute("SELECT * FROM summaries_fts WHERE summaries_fts MATCH 'Redis caching'").fetchall()
    assert len(rows) > 0
```
