"""Tests for entity extraction from events."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

from llm_mem.core.extraction import EntityExtractor
from llm_mem.core.ollama import OllamaClient
from llm_mem.core.queue import JobQueue
from llm_mem.models.config import Config

if TYPE_CHECKING:
    from llm_mem.core.database import Database
    pass


def _setup_engine_and_extractor(
    memory_db: Database,
) -> tuple:
    from llm_mem.core.engine import MemoryEngine

    config = Config(sensitive_data={"enabled": False, "llm_scan": False})
    engine = MemoryEngine(memory_db, config)
    extractor = EntityExtractor(memory_db, OllamaClient())
    return engine, extractor


def _assert_pending_job(db: Database, job_type: str) -> None:
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT id FROM jobs WHERE type = ? AND status = 'pending' LIMIT 1",
            (job_type,),
        ).fetchone()
        assert row is not None, f"No pending {job_type} job found"
    finally:
        conn.close()


class TestEntityExtractor:
    def test_extracts_decisions(self, memory_db: Database) -> None:
        engine, extractor = _setup_engine_and_extractor(memory_db)
        engine.start_session()
        event = engine.ingest_one(
            "response",
            "I recommend using Redis for caching because it is fast",
        )
        assert event is not None

        llm_response = (
            '{"decisions": [{"title": "Use Redis", "content": "Chose Redis for caching", '
            '"key_points": ["Redis chosen for caching", "Fast in-memory store"], '
            '"synopsis": "Decided to use Redis for the caching layer due to its speed."}],'
            '"todos": [], "facts": [], "failures": [], "discoveries": [], '
            '"features": [], "bugfixes": [], "research": [], "changes": []}'
        )
        with patch.object(extractor.ollama, "_generate", return_value=llm_response):
            entities = extractor.process_pending()

        assert len(entities) == 1
        assert entities[0].type == "decision"
        assert entities[0].title == "Use Redis"
        assert entities[0].key_points is not None
        assert "Redis chosen for caching" in entities[0].key_points
        assert entities[0].synopsis is not None
        assert "Redis" in entities[0].synopsis

    def test_extracts_todos(self, memory_db: Database) -> None:
        engine, extractor = _setup_engine_and_extractor(memory_db)
        engine.start_session()
        event = engine.ingest_one("response", "We need to add auth middleware")
        assert event is not None

        llm_response = (
            '{"decisions": [], "todos": ['
            '{"title": "Add auth middleware", "content": "Implement auth", '
            '"priority": "high", "status": "open"}],'
            '"facts": [], "failures": [], "discoveries": [], '
            '"features": [], "bugfixes": [], "research": [], "changes": []}'
        )
        with patch.object(extractor.ollama, "_generate", return_value=llm_response):
            entities = extractor.process_pending()

        assert len(entities) == 1
        assert entities[0].type == "todo"
        assert entities[0].priority == "high"

    def test_extracts_multiple_categories(
        self, memory_db: Database
    ) -> None:
        engine, extractor = _setup_engine_and_extractor(memory_db)
        engine.start_session()
        event = engine.ingest_one("response", "Decided to use FastAPI. Got a 500 error.")
        assert event is not None

        llm_response = (
            '{"decisions": [{"title": "Use FastAPI", "content": "Chose FastAPI"}],'
            '"todos": [],'
            '"facts": [],'
            '"failures": [{"title": "500 error", '
            '"content": "Got a server error", "status": "unresolved"}],'
            '"discoveries": [], '
            '"features": [], "bugfixes": [], "research": [], "changes": []}'
        )
        with patch.object(extractor.ollama, "_generate", return_value=llm_response):
            entities = extractor.process_pending()

        assert len(entities) == 2
        types = {e.type for e in entities}
        assert "decision" in types
        assert "failure" in types

    def test_entity_linked_to_source_event(
        self, memory_db: Database
    ) -> None:
        engine, extractor = _setup_engine_and_extractor(memory_db)
        engine.start_session()
        event = engine.ingest_one("response", "Use SQLite for storage")
        assert event is not None

        llm_response = (
            '{"decisions": [{"title": "Use SQLite", "content": "Storage"}],'
            '"todos": [], "facts": [], "failures": [], "discoveries": [], '
            '"features": [], "bugfixes": [], "research": [], "changes": []}'
        )
        with patch.object(extractor.ollama, "_generate", return_value=llm_response):
            entities = extractor.process_pending()

        assert len(entities) == 1
        assert entities[0].source_event_id == event.id

    def test_invalid_json_returns_empty(self, memory_db: Database) -> None:
        engine, extractor = _setup_engine_and_extractor(memory_db)
        engine.start_session()
        engine.ingest_one("response", "some text")

        with patch.object(
            extractor.ollama, "_generate", return_value="not valid json"
        ):
            entities = extractor.process_pending()

        assert entities == []

    def test_ollama_failure_retries_job(self, memory_db: Database) -> None:
        engine, extractor = _setup_engine_and_extractor(memory_db)
        engine.start_session()
        engine.ingest_one("response", "some content")

        with patch.object(
            extractor.ollama, "_generate", return_value=None
        ):
            entities = extractor.process_pending()

        assert entities == []

        queue = JobQueue(memory_db)
        assert queue.get_pending_count("extract_entities") == 0

        conn = memory_db.connect()
        try:
            row = conn.execute(
                "SELECT * FROM jobs WHERE type = 'extract_entities' LIMIT 1"
            ).fetchone()
            assert row is not None
            assert row["status"] == "failed"
            assert "Ollama returned no response" in row["error"]
        finally:
            conn.close()

    def test_no_pending_jobs_returns_empty(
        self, memory_db: Database
    ) -> None:
        _, extractor = _setup_engine_and_extractor(memory_db)
        entities = extractor.process_pending()
        assert entities == []

    def test_extracts_new_entity_types(
        self, memory_db: Database
    ) -> None:
        engine, extractor = _setup_engine_and_extractor(memory_db)
        engine.start_session()
        event = engine.ingest_one("response", "Added export feature, fixed a bug")
        assert event is not None

        llm_response = (
            '{"decisions": [], "todos": [], "facts": [], "failures": [], '
            '"discoveries": [], '
            '"features": [{"title": "Export feature", "content": "Added export"}], '
            '"bugfixes": [{"title": "Fixed bug", "content": "Fixed null pointer"}], '
            '"research": [], "changes": []}'
        )
        with patch.object(extractor.ollama, "_generate", return_value=llm_response):
            entities = extractor.process_pending()

        assert len(entities) == 2
        types = {e.type for e in entities}
        assert "feature" in types
        assert "bugfix" in types

    def test_key_points_fallback_to_content(
        self, memory_db: Database
    ) -> None:
        engine, extractor = _setup_engine_and_extractor(memory_db)
        engine.start_session()
        engine.ingest_one("response", "some content")

        llm_response = (
            '{"decisions": [{"title": "Test", "content": "The content"}],'
            '"todos": [], "facts": [], "failures": [], "discoveries": [], '
            '"features": [], "bugfixes": [], "research": [], "changes": []}'
        )
        with patch.object(extractor.ollama, "_generate", return_value=llm_response):
            entities = extractor.process_pending()

        assert len(entities) == 1
        assert entities[0].key_points is None
        assert entities[0].synopsis is None


class TestEventBusHandling:
    def test_no_error_when_event_bus_is_none(self, memory_db: Database) -> None:
        engine, extractor = _setup_engine_and_extractor(memory_db)
        assert extractor.event_bus is None
        engine.start_session()
        event = engine.ingest_one("response", "We chose PostgreSQL for the DB")
        assert event is not None

        llm_response = (
            '{"decisions": [{"title": "Use PostgreSQL", "content": "DB choice"}],'
            '"todos": [], "facts": [], "failures": [], "discoveries": [], '
            '"features": [], "bugfixes": [], "research": [], "changes": []}'
        )
        with patch.object(extractor.ollama, "_generate", return_value=llm_response):
            entities = extractor.process_pending()

        assert len(entities) == 1
        assert entities[0].type == "decision"

    def test_publish_called_when_event_bus_provided(self, memory_db: Database) -> None:
        from unittest.mock import MagicMock

        event_bus = MagicMock()
        engine, extractor = _setup_engine_and_extractor(memory_db)
        extractor.event_bus = event_bus
        engine.start_session()
        event = engine.ingest_one("response", "We decided on SQLite")
        assert event is not None

        llm_response = (
            '{"decisions": [{"title": "Use SQLite", "content": "Storage choice"}],'
            '"todos": [], "facts": [], "failures": [], "discoveries": [], '
            '"features": [], "bugfixes": [], "research": [], "changes": []}'
        )
        with patch.object(extractor.ollama, "_generate", return_value=llm_response):
            entities = extractor.process_pending()

        assert len(entities) == 1
        event_bus.publish.assert_called_once()
        call_args = event_bus.publish.call_args
        assert call_args[0][0] == "entity_created"
        assert call_args[0][1]["title"] == "Use SQLite"


class TestIngestQueuesExtraction:
    def test_ingest_creates_extraction_job(
        self, memory_db: Database
    ) -> None:
        engine, _ = _setup_engine_and_extractor(memory_db)
        engine.start_session()
        engine.ingest_one("note", "some event content")

        queue = JobQueue(memory_db)
        assert queue.get_pending_count("extract_entities") == 1

    def test_ingest_multiple_events_single_job(
        self, memory_db: Database
    ) -> None:
        engine, _ = _setup_engine_and_extractor(memory_db)
        engine.start_session()
        from llm_mem.models.events import EventInput

        engine.ingest([
            EventInput(type="note", content="event 1"),
            EventInput(type="note", content="event 2"),
        ])

        queue = JobQueue(memory_db)
        assert queue.get_pending_count("extract_entities") == 1

        job = queue.dequeue("extract_entities")
        assert job is not None
        assert len(job.payload["event_ids"]) == 2


class TestAutoResolution:
    def test_bugfix_resolves_matching_todo(self, memory_db: Database) -> None:
        engine, extractor = _setup_engine_and_extractor(memory_db)
        engine.start_session()

        from llm_mem.models.entities import Entity

        todo = Entity(
            project_id=engine.project_id,
            type="todo",
            title="Fix copy button clipboard fallback",
            content="The copy button fails on non-HTTPS",
            status="open",
            priority="high",
        )
        conn = memory_db.connect()
        row = todo.to_row()
        conn.execute(
            "INSERT INTO entities "
            "(id, project_id, source_event_id, type, title, content, "
            "status, priority, pinned, created_at, updated_at, "
            "resolved_at, metadata, archived_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row["id"], row["project_id"], row["source_event_id"],
                row["type"], row["title"], row["content"],
                row["status"], row["priority"], row["pinned"],
                row["created_at"], row["updated_at"],
                row["resolved_at"], row["metadata"], row["archived_at"],
            ),
        )
        conn.commit()

        engine.ingest_one("note", "Fixed the copy button clipboard fallback")

        _assert_pending_job(memory_db, "extract_entities")

        llm_response = (
            '{"decisions": [], "todos": [], "facts": [], "failures": [], '
            '"discoveries": [], "features": [], '
            '"bugfixes": [{"title": "Fixed copy button clipboard fallback", '
            '"content": "Added fallback copy mechanism", '
            '"key_points": ["navigator.clipboard fails on non-HTTPS"], '
            '"synopsis": "Fixed by adding execCommand fallback"}], '
            '"research": [], "changes": []}'
        )

        with patch.object(extractor.ollama, "_generate", return_value=llm_response):
            entities = extractor.process_pending()

        assert len(entities) == 1
        assert entities[0].type == "bugfix"

        updated = conn.execute(
            "SELECT status FROM entities WHERE id = ?", (todo.id,)
        ).fetchone()
        conn.close()
        assert updated["status"] == "done"

    def test_feature_resolves_matching_todo(self, memory_db: Database) -> None:
        engine, extractor = _setup_engine_and_extractor(memory_db)
        engine.start_session()

        from llm_mem.models.entities import Entity

        todo = Entity(
            project_id=engine.project_id,
            type="todo",
            title="Implement analysis history selector",
            content="Need a dropdown to pick past analysis runs",
            status="open",
            priority="medium",
        )
        conn = memory_db.connect()
        row = todo.to_row()
        conn.execute(
            "INSERT INTO entities "
            "(id, project_id, source_event_id, type, title, content, "
            "status, priority, pinned, created_at, updated_at, "
            "resolved_at, metadata, archived_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row["id"], row["project_id"], row["source_event_id"],
                row["type"], row["title"], row["content"],
                row["status"], row["priority"], row["pinned"],
                row["created_at"], row["updated_at"],
                row["resolved_at"], row["metadata"], row["archived_at"],
            ),
        )
        conn.commit()

        engine.ingest_one("note", "Built the analysis history selector")

        _assert_pending_job(memory_db, "extract_entities")

        llm_response = (
            '{"decisions": [], "todos": [], "facts": [], "failures": [], '
            '"discoveries": [], '
            '"features": [{"title": "Analysis history selector implemented", '
            '"content": "Dropdown to pick past runs", '
            '"key_points": ["Uses analysis_results table"], '
            '"synopsis": "Implemented UI component"}], '
            '"bugfixes": [], "research": [], "changes": []}'
        )

        with patch.object(extractor.ollama, "_generate", return_value=llm_response):
            extractor.process_pending()

        updated = conn.execute(
            "SELECT status FROM entities WHERE id = ?", (todo.id,)
        ).fetchone()
        conn.close()
        assert updated["status"] == "done"

    def test_no_resolve_when_unrelated(self, memory_db: Database) -> None:
        engine, extractor = _setup_engine_and_extractor(memory_db)
        engine.start_session()

        from llm_mem.models.entities import Entity

        todo = Entity(
            project_id=engine.project_id,
            type="todo",
            title="Configure Redis caching layer",
            content="Set up Redis for session storage",
            status="open",
            priority="medium",
        )
        conn = memory_db.connect()
        row = todo.to_row()
        conn.execute(
            "INSERT INTO entities "
            "(id, project_id, source_event_id, type, title, content, "
            "status, priority, pinned, created_at, updated_at, "
            "resolved_at, metadata, archived_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row["id"], row["project_id"], row["source_event_id"],
                row["type"], row["title"], row["content"],
                row["status"], row["priority"], row["pinned"],
                row["created_at"], row["updated_at"],
                row["resolved_at"], row["metadata"], row["archived_at"],
            ),
        )
        conn.commit()

        engine.ingest_one("note", "Fixed the copy button clipboard fallback")
        _assert_pending_job(memory_db, "extract_entities")

        llm_response = (
            '{"decisions": [], "todos": [], "facts": [], "failures": [], '
            '"discoveries": [], "features": [], '
            '"bugfixes": [{"title": "Fixed copy button clipboard fallback", '
            '"content": "Added fallback copy mechanism", '
            '"key_points": ["navigator.clipboard fails"], '
            '"synopsis": "Fixed"}], '
            '"research": [], "changes": []}'
        )

        with patch.object(extractor.ollama, "_generate", return_value=llm_response):
            extractor.process_pending()

        updated = conn.execute(
            "SELECT status FROM entities WHERE id = ?", (todo.id,)
        ).fetchone()
        conn.close()
        assert updated["status"] == "open"
