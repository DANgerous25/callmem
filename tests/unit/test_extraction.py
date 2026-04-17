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
