# WO-07: Retrieval Engine and Startup Briefing

## Objective

Implement the retrieval engine that combines structured lookup, FTS5 search, and recency to find relevant memories. Implement the briefing generator that composes a startup context block.

## Files to create

- `src/callmem/core/retrieval.py` — Multi-strategy retrieval engine
- `src/callmem/core/briefing.py` — Briefing assembly and compression
- `tests/unit/test_retrieval.py`
- `tests/unit/test_briefing.py`

## Files to modify

- `src/callmem/core/engine.py` — Add `search()` and `get_briefing()` methods that delegate to retrieval/briefing modules
- `src/callmem/mcp/tools.py` — Wire `mem_search` to use retrieval engine, `mem_get_briefing` to use briefing generator

## Constraints

- Retrieval is synchronous
- Results from all strategies are merged and deduplicated by ID
- Each result has a composite score: `weight * strategy_score * recency_factor`
- Recency factor: exponential decay — recent items score higher
- Token estimation: use simple `len(text) / 4` heuristic (good enough for planning)
- Briefing must fit within configured token budget
- If briefing exceeds budget, use Ollama to compress (if available) — otherwise truncate lower-priority sections
- Briefing sections have priority: active TODOs > unresolved failures > recent decisions > pinned facts > last session summary > project summary

## Retrieval engine interface

```python
@dataclass
class SearchResult:
    id: str
    source_type: str  # event, entity, summary
    type: str         # prompt, decision, todo, etc.
    title: str | None
    content: str
    score: float
    timestamp: str
    session_id: str | None
    metadata: dict | None

class RetrievalEngine:
    def __init__(self, repo: Repository, config: Config): ...

    def search(
        self,
        query: str,
        types: list[str] | None = None,
        session_id: str | None = None,
        limit: int = 20,
        include_archived: bool = False,
        strategies: list[str] | None = None,  # Override default strategies
    ) -> list[SearchResult]: ...

    def get_recent(
        self,
        limit: int = 20,
        session_id: str | None = None,
    ) -> list[SearchResult]: ...
```

## Briefing generator interface

```python
@dataclass
class Briefing:
    project_name: str
    content: str          # Formatted markdown
    token_count: int
    components: dict      # Which sections were included and their sizes
    generated_at: str

class BriefingGenerator:
    def __init__(self, repo: Repository, config: Config, ollama: OllamaClient | None): ...

    def generate(
        self,
        project_id: str,
        max_tokens: int | None = None,
        focus: str | None = None,
    ) -> Briefing: ...
```

## Briefing format

```markdown
## Session Briefing — {project_name}

### Active TODOs
- [ ] {todo_title} ({priority}) — {short_content}
...

### Recent Decisions
- {decision_title}: {short_content} ({when})
...

### Unresolved Issues
- {failure_title}: {short_content}
...

### Key Facts
- {fact_title}: {short_content}
...

### Last Session
{session_summary}

### Project Context
{project_summary}
```

## Acceptance criteria

1. `retrieval.search("pagination")` returns events and entities matching the keyword
2. Results are ranked by composite score (not just alphabetical or chronological)
3. Structured lookup works: `search(query="", types=["todo"])` returns all TODOs
4. Recency weighting works: recent results score higher than old ones
5. Deduplication works: same event doesn't appear twice even if matched by multiple strategies
6. `briefing.generate(project_id)` returns a formatted markdown briefing
7. Briefing respects token budget — output is never larger than `max_tokens`
8. Briefing with no prior data returns a "new project" message
9. Focus parameter narrows the briefing to relevant items
10. `pytest tests/unit/test_retrieval.py tests/unit/test_briefing.py` passes

## Suggested tests

```python
# Retrieval
def test_fts5_search(engine_with_data):
    results = engine_with_data.search("cursor pagination")
    assert len(results) > 0
    assert any("pagination" in r.content.lower() for r in results)

def test_structured_search_by_type(engine_with_data):
    results = engine_with_data.search("", types=["todo"])
    assert all(r.type == "todo" for r in results)

def test_recency_ranking(engine_with_data):
    results = engine_with_data.search("test")
    # Most recent result should have highest score
    assert results[0].score >= results[-1].score

def test_deduplication(engine_with_data):
    results = engine_with_data.search("Redis")
    ids = [r.id for r in results]
    assert len(ids) == len(set(ids))

# Briefing
def test_briefing_includes_todos(engine_with_data):
    briefing = engine_with_data.get_briefing()
    assert "Active TODOs" in briefing.content

def test_briefing_respects_token_budget(engine_with_lots_of_data):
    briefing = engine_with_lots_of_data.get_briefing(max_tokens=500)
    assert briefing.token_count <= 500

def test_briefing_new_project(empty_engine):
    briefing = empty_engine.get_briefing()
    assert "new project" in briefing.content.lower() or "no prior" in briefing.content.lower()

def test_briefing_with_focus(engine_with_data):
    briefing = engine_with_data.get_briefing(focus="authentication")
    # Should prioritize auth-related items
    assert "auth" in briefing.content.lower()
```
