"""Tests for the MemoryEngine."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from callmem.core.engine import MemoryEngine
from callmem.models.config import Config
from callmem.models.events import EventInput

if TYPE_CHECKING:
    from callmem.core.database import Database


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
        assert event.metadata is not None
        assert event.metadata["tool"] == "pytest"
        assert "scan_status" in event.metadata


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
        from callmem.core.engine import DEFAULT_MAX_EVENT_SIZE

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


class TestToolFiltering:
    def _engine_with_filters(
        self,
        memory_db: Database,
        skip_tools: list[str] | None = None,
        skip_patterns: list[str] | None = None,
    ) -> MemoryEngine:
        config = Config(
            ingestion={
                "skip_tools": skip_tools or [],
                "skip_patterns": skip_patterns or [],
            },
        )
        return MemoryEngine(memory_db, config)

    def test_skip_tools_drops_matching_call(
        self, memory_db: Database,
    ) -> None:
        engine = self._engine_with_filters(memory_db, skip_tools=["Glob"])
        engine.start_session()
        stored = engine.ingest([
            EventInput(type="tool_call", content='Glob({"pattern":"*.py"})'),
            EventInput(type="tool_call", content='Read({"path":"a.py"})'),
        ])
        assert len(stored) == 1
        assert stored[0].content.startswith("Read(")
        assert engine.ingestion_stats()["skipped_tool_calls"] == 1

    def test_skip_patterns_glob_match(self, memory_db: Database) -> None:
        engine = self._engine_with_filters(
            memory_db, skip_patterns=["Read(*node_modules*)"],
        )
        engine.start_session()
        stored = engine.ingest([
            EventInput(
                type="tool_call",
                content='Read({"path":"./node_modules/foo.js"})',
            ),
            EventInput(
                type="tool_call",
                content='Read({"path":"src/main.py"})',
            ),
        ])
        assert len(stored) == 1
        assert "main.py" in stored[0].content
        assert engine.ingestion_stats()["skipped_tool_calls"] == 1

    def test_non_tool_events_never_skipped(
        self, memory_db: Database,
    ) -> None:
        engine = self._engine_with_filters(
            memory_db,
            skip_tools=["prompt", "response"],
            skip_patterns=["*"],
        )
        engine.start_session()
        stored = engine.ingest([
            EventInput(type="prompt", content="prompt body"),
            EventInput(type="response", content="response body"),
        ])
        assert len(stored) == 2
        assert engine.ingestion_stats()["skipped_tool_calls"] == 0

    def test_default_config_skips_nothing(self, engine: MemoryEngine) -> None:
        engine.start_session()
        stored = engine.ingest([
            EventInput(type="tool_call", content='Glob({"pattern":"*.py"})'),
        ])
        assert len(stored) == 1
        assert engine.ingestion_stats()["skipped_tool_calls"] == 0


class TestFileContext:
    def _seed_entity_linked_to(
        self, engine: MemoryEngine, file_path: str,
        entity_type: str = "feature",
        title: str = "Implemented something",
        synopsis: str | None = None,
        created_at: str | None = None,
    ) -> str:
        from callmem.models.entities import Entity

        entity = Entity(
            project_id=engine.project_id,
            source_event_id=None,
            type=entity_type,
            title=title,
            content=title,
            synopsis=synopsis,
        )
        if created_at is not None:
            entity.created_at = created_at
            entity.updated_at = created_at
        row = entity.to_row()
        conn = engine.db.connect()
        try:
            conn.execute(
                "INSERT INTO entities "
                "(id, project_id, source_event_id, type, title, content, "
                "key_points, synopsis, status, priority, pinned, "
                "created_at, updated_at, resolved_at, metadata, archived_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    row["id"], row["project_id"], row["source_event_id"],
                    row["type"], row["title"], row["content"],
                    row["key_points"], row["synopsis"], row["status"],
                    row["priority"], row["pinned"], row["created_at"],
                    row["updated_at"], row["resolved_at"], row["metadata"],
                    row["archived_at"],
                ),
            )
            conn.execute(
                "INSERT INTO entity_files (entity_id, file_path, relation) "
                "VALUES (?, ?, 'related')",
                (row["id"], file_path),
            )
            conn.commit()
        finally:
            conn.close()
        return row["id"]

    def test_unknown_file_returns_no_observations(
        self, engine: MemoryEngine,
    ) -> None:
        result = engine.get_file_context("src/never_touched.py")
        assert result["has_observations"] is False
        assert result["observation_count"] == 0
        assert result["timeline"] == []
        assert engine.file_context_stats() == {
            "calls": 1, "hits": 0, "misses": 1,
        }

    def test_known_file_returns_ordered_timeline(
        self, engine: MemoryEngine,
    ) -> None:
        path = "src/auth/middleware.py"
        self._seed_entity_linked_to(
            engine, path, entity_type="feature",
            title="Created JWT middleware", created_at="2026-04-01T10:00:00",
        )
        self._seed_entity_linked_to(
            engine, path, entity_type="bugfix",
            title="Fixed token expiry", created_at="2026-04-05T12:00:00",
            synopsis="Off-by-one in expiry check",
        )

        result = engine.get_file_context(path)
        assert result["has_observations"] is True
        assert result["observation_count"] == 2
        assert result["first_seen"] == "2026-04-01"
        assert result["last_modified"] == "2026-04-05"
        assert [row["type"] for row in result["timeline"]] == [
            "feature", "bugfix",
        ]
        assert engine.file_context_stats()["hits"] == 1

    def test_basename_fallback_matches_different_relative_path(
        self, engine: MemoryEngine,
    ) -> None:
        self._seed_entity_linked_to(
            engine, "src/auth/middleware.py",
            title="Only match via basename",
        )
        result = engine.get_file_context("./middleware.py")
        assert result["has_observations"] is True
        assert result["observation_count"] == 1

    def test_include_content_returns_file_bytes_when_present(
        self, engine: MemoryEngine, tmp_path,
    ) -> None:
        real_file = tmp_path / "example.txt"
        real_file.write_text("hello from disk")
        result = engine.get_file_context(
            str(real_file), include_content=True,
        )
        assert result["has_observations"] is False
        assert result["current_content"] == "hello from disk"


class TestEndlessMode:
    def test_check_context_under_threshold_returns_ok(
        self, memory_db: Database,
    ) -> None:
        config = Config(
            endless_mode={"enabled": True, "context_limit": 8000},
        )
        engine = MemoryEngine(memory_db, config)
        result = engine.check_context(
            message_count=5, estimated_tokens=1000,
        )
        assert result["status"] == "ok"

    def test_check_context_over_threshold_recommends_compression(
        self, memory_db: Database,
    ) -> None:
        config = Config(
            endless_mode={
                "enabled": True,
                "context_limit": 8000,
                "compress_threshold": 0.8,
            },
        )
        engine = MemoryEngine(memory_db, config)
        result = engine.check_context(
            message_count=200, estimated_tokens=7000,
        )
        assert result["status"] == "compress_recommended"
        assert result["usage_ratio"] >= 0.8
        assert "compress" in result["action"].lower()

    def test_check_context_disabled_never_recommends(
        self, memory_db: Database,
    ) -> None:
        config = Config(
            endless_mode={"enabled": False, "context_limit": 8000},
        )
        engine = MemoryEngine(memory_db, config)
        result = engine.check_context(
            message_count=10_000, estimated_tokens=1_000_000,
        )
        assert result["status"] == "disabled"

    def test_compress_context_persists_summary_and_marker(
        self, memory_db: Database,
    ) -> None:
        engine = MemoryEngine(memory_db, Config())
        session = engine.start_session()
        result = engine.compress_context(
            summary="Agent summary of messages 1-30: decided to use JWT.",
            message_range="messages 1-30",
        )
        assert result["status"] == "compressed"
        assert result["session_id"] == session.id
        assert result["compression_events"] == 1
        assert "compressed" in result["marker"].lower()

        conn = memory_db.connect()
        try:
            row = conn.execute(
                "SELECT content, session_id FROM summaries "
                "WHERE id = ?",
                (result["summary_id"],),
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        assert row["session_id"] == session.id
        assert "JWT" in row["content"]

        refreshed = engine.get_session(session.id)
        assert refreshed is not None
        assert refreshed.metadata is not None
        assert refreshed.metadata["compression_events"] == 1

    def test_compress_context_without_active_session_errors(
        self, memory_db: Database,
    ) -> None:
        engine = MemoryEngine(memory_db, Config())
        with pytest.raises(ValueError, match="No active session"):
            engine.compress_context(summary="anything")

    def test_compress_context_rejects_empty_summary(
        self, memory_db: Database,
    ) -> None:
        engine = MemoryEngine(memory_db, Config())
        engine.start_session()
        with pytest.raises(ValueError, match="summary is required"):
            engine.compress_context(summary="   ")


class TestProjectAutoCreation:
    def test_project_created_on_first_use(self, engine: MemoryEngine) -> None:
        assert engine.project_id is not None
        from callmem.core.repository import Repository

        repo = Repository(engine.db)
        project = repo.get_project(engine.project_id)
        assert project is not None
        assert project.name == "default"

    def test_project_name_from_config(self, memory_db: Database) -> None:
        config = Config(project={"name": "my-app"})
        engine = MemoryEngine(memory_db, config)
        assert engine.project_id is not None
        from callmem.core.repository import Repository

        repo = Repository(engine.db)
        project = repo.get_project(engine.project_id)
        assert project is not None
        assert project.name == "my-app"
