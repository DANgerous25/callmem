# WO-02: Data Models and Type System

## Objective

Create Pydantic data models for all core types: events, sessions, entities, summaries, projects, and memory edges. These models are the shared contract between all subsystems.

## Files to create

- `src/callmem/models/__init__.py` — Re-export all models
- `src/callmem/models/events.py` — Event types and models
- `src/callmem/models/sessions.py` — Session model
- `src/callmem/models/entities.py` — Entity types (decision, todo, fact, failure, discovery)
- `src/callmem/models/summaries.py` — Summary model
- `src/callmem/models/projects.py` — Project model
- `src/callmem/models/edges.py` — Memory edge model
- `tests/unit/test_models.py`

## Constraints

- All models use Pydantic v2 (`BaseModel`)
- IDs are generated via `python-ulid` at creation time
- Timestamps are ISO 8601 strings (not datetime objects — avoids timezone confusion in SQLite)
- Use `Literal` types for enums (not Python `Enum` class — simpler serialization)
- Every model must have `to_row() -> dict` for SQLite insertion and `from_row(row: dict) -> Self` classmethod for hydration
- Use strict type validation

## Key types

```python
EventType = Literal["prompt", "response", "tool_call", "file_change", "decision", "todo", "failure", "discovery", "fact", "note"]
EntityType = Literal["decision", "todo", "fact", "failure", "discovery"]
EntityStatus = Literal["open", "done", "cancelled", "unresolved", "resolved"]
SessionStatus = Literal["active", "ended", "abandoned"]
SummaryLevel = Literal["chunk", "session", "cross_session"]
EdgeRelation = Literal["caused_by", "relates_to", "supersedes", "resolves", "blocks"]
Priority = Literal["high", "medium", "low"]
```

## Acceptance criteria

1. All models can be instantiated with valid data
2. All models reject invalid data (wrong types, missing required fields)
3. `to_row()` produces a dict suitable for SQLite parameterized insert
4. `from_row()` reconstructs the model from a SQLite row dict
5. ULID IDs are auto-generated when not provided
6. Timestamps default to current time when not provided
7. `pytest tests/unit/test_models.py` passes

## Suggested tests

```python
def test_event_creation():
    event = Event(session_id="...", project_id="...", type="prompt", content="Hello")
    assert event.id is not None  # Auto-generated ULID
    assert event.timestamp is not None
    assert event.type == "prompt"

def test_event_round_trip():
    event = Event(session_id="...", project_id="...", type="tool_call", content="ran tests")
    row = event.to_row()
    reconstructed = Event.from_row(row)
    assert reconstructed == event

def test_entity_with_status():
    todo = Entity(project_id="...", type="todo", title="Fix bug", content="...", status="open", priority="high")
    assert todo.pinned == False
    assert todo.status == "open"

def test_invalid_event_type_rejected():
    with pytest.raises(ValidationError):
        Event(session_id="...", project_id="...", type="invalid", content="Hello")
```
