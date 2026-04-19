# WO-04: Core Engine — Ingest and Session Management

## Objective

Implement the core engine that manages sessions and ingests events. This is the central module that all adapters (MCP, REST, direct) call.

## Files to create

- `src/callmem/core/engine.py` — Main engine class with ingest and session methods
- `src/callmem/core/repository.py` — Data access layer (SQL queries abstracted behind methods)
- `tests/unit/test_engine.py`
- `tests/unit/test_repository.py`

## Constraints

- Engine is synchronous for v1 (async adapter layer can wrap later)
- Repository pattern: engine calls repository methods, never writes SQL directly
- Repository uses parameterized queries only (no string formatting of SQL)
- Session auto-creation: if no active session exists and `auto_start` is enabled, create one on first ingest
- Event deduplication: ignore events with identical content within `dedup_window_s`
- Event size limiting: truncate content exceeding `max_event_size_chars`
- FTS5 sync happens via SQLite triggers (already in schema), not in Python code

## Key methods

```python
class MemoryEngine:
    def __init__(self, db: Database, config: Config): ...

    # Session management
    def start_session(self, agent_name: str = None, model_name: str = None) -> Session: ...
    def end_session(self, session_id: str, note: str = None) -> Session: ...
    def get_active_session(self) -> Session | None: ...
    def get_session(self, session_id: str) -> Session | None: ...
    def list_sessions(self, limit: int = 20, offset: int = 0) -> list[Session]: ...

    # Ingest
    def ingest(self, events: list[EventInput]) -> list[Event]: ...
    def ingest_one(self, type: EventType, content: str, metadata: dict = None) -> Event: ...

    # Read
    def get_events(self, session_id: str = None, type: EventType = None, limit: int = 50) -> list[Event]: ...
    def get_event(self, event_id: str) -> Event | None: ...
```

```python
class Repository:
    def __init__(self, db: Database): ...

    # Projects
    def create_project(self, project: Project) -> None: ...
    def get_project(self, project_id: str) -> Project | None: ...
    def get_project_by_name(self, name: str) -> Project | None: ...

    # Sessions
    def insert_session(self, session: Session) -> None: ...
    def update_session(self, session: Session) -> None: ...
    def get_active_session(self, project_id: str) -> Session | None: ...
    def list_sessions(self, project_id: str, limit: int, offset: int) -> list[Session]: ...

    # Events
    def insert_event(self, event: Event) -> None: ...
    def insert_events(self, events: list[Event]) -> None: ...
    def get_events(self, project_id: str, session_id: str = None, type: str = None, limit: int = 50) -> list[Event]: ...
    def count_events(self, project_id: str, session_id: str = None) -> int: ...
```

## Acceptance criteria

1. `engine.start_session()` creates a session row in the database
2. `engine.ingest([...])` stores events and increments session event count
3. `engine.end_session(id)` sets `ended_at` and status
4. Auto-session creation works when configured
5. Duplicate events within the dedup window are silently dropped
6. Oversized events are truncated with a marker
7. FTS5 index is populated (verify with a raw FTS query after ingest)
8. All repository methods work correctly
9. `pytest tests/unit/test_engine.py tests/unit/test_repository.py` passes

## Suggested tests

```python
def test_session_lifecycle(engine):
    session = engine.start_session(agent_name="test")
    assert session.status == "active"
    engine.ingest_one("prompt", "Hello world")
    ended = engine.end_session(session.id)
    assert ended.status == "ended"
    assert ended.event_count == 1

def test_ingest_creates_events(engine):
    engine.start_session()
    events = engine.ingest([
        EventInput(type="prompt", content="Fix the bug"),
        EventInput(type="response", content="I'll look into it"),
    ])
    assert len(events) == 2
    assert events[0].type == "prompt"

def test_fts5_populated_after_ingest(engine, db):
    engine.start_session()
    engine.ingest_one("prompt", "implement cursor-based pagination")
    rows = db.execute("SELECT * FROM events_fts WHERE events_fts MATCH 'pagination'").fetchall()
    assert len(rows) == 1

def test_dedup_within_window(engine):
    engine.start_session()
    engine.ingest_one("prompt", "same content")
    engine.ingest_one("prompt", "same content")  # Within dedup window
    events = engine.get_events()
    assert len(events) == 1

def test_auto_session_creation(engine_with_auto_start):
    # No session started explicitly
    engine_with_auto_start.ingest_one("prompt", "Hello")
    session = engine_with_auto_start.get_active_session()
    assert session is not None
```
