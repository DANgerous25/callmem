"""Tests for the MemoryEngine."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from llm_mem.core.engine import MemoryEngine
from llm_mem.models.config import Config
from llm_mem.models.events import EventInput

if TYPE_CHECKING:
    from llm_mem.core.database import Database


class TestSessionLifecycle:
    def test_start_session(self, engine: MemoryEngine) -> None:
        session = engine.start_session(agent_name="test")
        assert session.status == "active"
        assert session.agent_name == "test"
        assert session.id is not None

    def test_end_session(self, engine: MemoryEngine) -> None:
        session = engine.start_session(agent_name="test")
        engine.ingest_one("prompt", "Hello world")
        ended = engine.end_session(session.id)
        assert ended.status == "ended"
        assert ended.event_count == 1
        assert ended.ended_at is not None

    def test_end_session_with_note(self, engine: MemoryEngine) -> None:
        session = engine.start_session()
        ended = engine.end_session(session.id, note="wrapped up auth work")
        assert ended.summary == "wrapped up auth work"

    def test_end_nonexistent_session(self, engine: MemoryEngine) -> None:
        with pytest.raises(ValueError, match="Session not found"):
            engine.end_session("nonexistent")

    def test_end_already_ended_session(self, engine: MemoryEngine) -> None:
        session = engine.start_session()
        engine.end_session(session.id)
        with pytest.raises(ValueError, match="not active"):
            engine.end_session(session.id)

    def test_get_active_session(self, engine: MemoryEngine) -> None:
        assert engine.get_active_session() is None
        session = engine.start_session()
        active = engine.get_active_session()
        assert active is not None
        assert active.id == session.id

    def test_get_session(self, engine: MemoryEngine) -> None:
        session = engine.start_session()
        fetched = engine.get_session(session.id)
        assert fetched is not None
        assert fetched.id == session.id

    def test_list_sessions(self, engine: MemoryEngine) -> None:
        engine.start_session()
        engine.start_session()
        sessions = engine.list_sessions()
        assert len(sessions) == 2

    def test_list_sessions_with_limit(self, engine: MemoryEngine) -> None:
        for _ in range(5):
            engine.start_session()
        assert len(engine.list_sessions(limit=3)) == 3


class TestIngest:
    def test_ingest_creates_events(self, engine: MemoryEngine) -> None:
        engine.start_session()
        events = engine.ingest([
            EventInput(type="prompt", content="Fix the bug"),
            EventInput(type="response", content="I'll look into it"),
        ])
        assert len(events) == 2
        assert events[0].type == "prompt"
        assert events[1].type == "response"

    def test_ingest_one(self, engine: MemoryEngine) -> None:
        engine.start_session()
        event = engine.ingest_one("prompt", "Hello world")
        assert event is not None
        assert event.content == "Hello world"
        assert event.type == "prompt"

    def test_ingest_empty_list(self, engine: MemoryEngine) -> None:
        result = engine.ingest([])
        assert result == []

    def test_ingest_increments_event_count(self, engine: MemoryEngine) -> None:
        session = engine.start_session()
        engine.ingest([
            EventInput(type="prompt", content="A"),
            EventInput(type="response", content="B"),
        ])
        fetched = engine.get_session(session.id)
        assert fetched is not None
        assert fetched.event_count == 2

    def test_ingest_with_metadata(self, engine: MemoryEngine) -> None:
        engine.start_session()
        event = engine.ingest_one(
            "tool_call", "ran pytest", metadata={"tool": "pytest"}
        )
        assert event is not None
        assert event.metadata == {"tool": "pytest"}


class TestAutoSession:
    def test_auto_session_on_ingest(self, engine: MemoryEngine) -> None:
        assert engine.get_active_session() is None
        event = engine.ingest_one("prompt", "Hello")
        assert event is not None
        session = engine.get_active_session()
        assert session is not None

    def test_auto_session_reuses_existing(self, engine: MemoryEngine) -> None:
        first = engine.start_session()
        engine.ingest_one("prompt", "Hello")
        assert engine.get_active_session() is not None
        assert engine.get_active_session().id == first.id


class TestDedup:
    def test_dedup_within_window(self, engine: MemoryEngine) -> None:
        engine.start_session()
        e1 = engine.ingest_one("prompt", "same content")
        e2 = engine.ingest_one("prompt", "same content")
        assert e1 is not None
        assert e2 is None  # duplicate dropped
        events = engine.get_events()
        assert len(events) == 1

    def test_different_content_not_deduped(self, engine: MemoryEngine) -> None:
        engine.start_session()
        engine.ingest_one("prompt", "content A")
        engine.ingest_one("prompt", "content B")
        events = engine.get_events()
        assert len(events) == 2

    def test_different_type_not_deduped(self, engine: MemoryEngine) -> None:
        engine.start_session()
        engine.ingest_one("prompt", "same content")
        engine.ingest_one("response", "same content")
        events = engine.get_events()
        assert len(events) == 2


class TestTruncation:
    def test_long_event_truncated(self, engine: MemoryEngine) -> None:
        from llm_mem.core.engine import DEFAULT_MAX_EVENT_SIZE

        engine.start_session()
        long_content = "x" * (DEFAULT_MAX_EVENT_SIZE + 1000)
        event = engine.ingest_one("note", long_content)
        assert event is not None
        assert len(event.content) <= DEFAULT_MAX_EVENT_SIZE + 100
        assert "[... truncated" in event.content

    def test_normal_event_not_truncated(self, engine: MemoryEngine) -> None:
        engine.start_session()
        content = "short content"
        event = engine.ingest_one("note", content)
        assert event is not None
        assert event.content == content


class TestRead:
    def test_get_events(self, engine: MemoryEngine) -> None:
        engine.start_session()
        engine.ingest([
            EventInput(type="prompt", content="A"),
            EventInput(type="response", content="B"),
        ])
        events = engine.get_events()
        assert len(events) == 2

    def test_get_events_by_session(self, engine: MemoryEngine) -> None:
        s1 = engine.start_session()
        engine.ingest_one("prompt", "In session 1")
        engine.end_session(s1.id)
        engine.start_session()
        engine.ingest_one("prompt", "In session 2")

        events_s1 = engine.get_events(session_id=s1.id)
        assert len(events_s1) == 1
        assert events_s1[0].content == "In session 1"

    def test_get_events_by_type(self, engine: MemoryEngine) -> None:
        engine.start_session()
        engine.ingest([
            EventInput(type="prompt", content="A"),
            EventInput(type="response", content="B"),
            EventInput(type="prompt", content="C"),
        ])
        prompts = engine.get_events(type="prompt")
        assert len(prompts) == 2

    def test_get_event_by_id(self, engine: MemoryEngine) -> None:
        engine.start_session()
        event = engine.ingest_one("prompt", "Find me")
        assert event is not None
        fetched = engine.get_event(event.id)
        assert fetched is not None
        assert fetched.content == "Find me"

    def test_get_event_not_found(self, engine: MemoryEngine) -> None:
        assert engine.get_event("nonexistent") is None


class TestFTS5:
    def test_fts5_populated_after_ingest(
        self, memory_db: Database, engine: MemoryEngine
    ) -> None:
        engine.start_session()
        engine.ingest_one("prompt", "implement cursor-based pagination for the API")

        conn = memory_db.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM events_fts WHERE events_fts MATCH 'pagination'"
            ).fetchall()
            assert len(rows) >= 1
        finally:
            conn.close()

    def test_fts5_multiple_events_searchable(
        self, memory_db: Database, engine: MemoryEngine
    ) -> None:
        engine.start_session()
        engine.ingest([
            EventInput(type="prompt", content="fix the authentication bug"),
            EventInput(type="response", content="the auth module needs tests"),
            EventInput(type="note", content="deployed to production"),
        ])

        conn = memory_db.connect()
        try:
            auth_rows = conn.execute(
                "SELECT * FROM events_fts WHERE events_fts MATCH 'auth*'"
            ).fetchall()
            assert len(auth_rows) >= 2

            prod_rows = conn.execute(
                "SELECT * FROM events_fts WHERE events_fts MATCH 'production'"
            ).fetchall()
            assert len(prod_rows) >= 1
        finally:
            conn.close()


class TestProjectAutoCreation:
    def test_project_created_on_first_use(self, engine: MemoryEngine) -> None:
        assert engine.project_id is not None
        from llm_mem.core.repository import Repository

        repo = Repository(engine.db)
        project = repo.get_project(engine.project_id)
        assert project is not None
        assert project.name == "default"

    def test_project_name_from_config(self, memory_db: Database) -> None:
        config = Config(project={"name": "my-app"})
        engine = MemoryEngine(memory_db, config)
        assert engine.project_id is not None
        from llm_mem.core.repository import Repository

        repo = Repository(engine.db)
        project = repo.get_project(engine.project_id)
        assert project is not None
        assert project.name == "my-app"
