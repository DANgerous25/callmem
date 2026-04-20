"""End-to-end integration tests for the ingest → extract → auto-resolve pipeline.

Unit tests cover each stage in isolation. These tests wire the full
flow together with a stubbed Ollama so regressions in how the stages
hand off (queue payload shape, project_id propagation, auto-resolve
ordering) surface here rather than in production.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import patch

from callmem.core.engine import MemoryEngine
from callmem.core.extraction import EntityExtractor
from callmem.core.ollama import OllamaClient
from callmem.models.config import Config

if TYPE_CHECKING:
    from callmem.core.database import Database


def _extraction_json(
    todos: list[dict] | None = None,
    features: list[dict] | None = None,
    bugfixes: list[dict] | None = None,
    changes: list[dict] | None = None,
) -> str:
    """Build an extraction-prompt response payload."""
    payload = {
        "decisions": [], "todos": todos or [], "facts": [],
        "failures": [], "discoveries": [], "features": features or [],
        "bugfixes": bugfixes or [], "research": [], "changes": changes or [],
    }
    return json.dumps(payload)


def _make_engine_and_extractor(
    memory_db: Database,
) -> tuple[MemoryEngine, EntityExtractor]:
    config = Config(sensitive_data={"enabled": False, "llm_scan": False})
    engine = MemoryEngine(memory_db, config)
    extractor = EntityExtractor(memory_db, OllamaClient())
    return engine, extractor


class TestIngestToExtract:
    """Events ingested should produce the expected entities after extraction."""

    def test_ingest_then_extract_creates_entity(
        self, memory_db: Database,
    ) -> None:
        engine, extractor = _make_engine_and_extractor(memory_db)
        engine.start_session()
        engine.ingest_one(
            "decision",
            "Use SQLite WAL mode for better write concurrency.",
        )

        response = _extraction_json(features=[{
            "title": "SQLite WAL mode enabled",
            "content": "Switched to WAL for concurrent writes.",
            "key_points": ["writer cooperates with readers"],
            "synopsis": "WAL replaces rollback journal.",
        }])
        with patch.object(extractor.ollama, "_generate", return_value=response):
            entities = extractor.process_pending()

        assert len(entities) == 1
        assert entities[0].type == "feature"
        assert entities[0].project_id == engine.project_id

    def test_extraction_attaches_to_correct_session(
        self, memory_db: Database,
    ) -> None:
        engine, extractor = _make_engine_and_extractor(memory_db)
        session = engine.start_session()
        event = engine.ingest_one("note", "implemented the X widget")
        assert event is not None

        response = _extraction_json(features=[{
            "title": "X widget implemented", "content": "Built X.",
            "key_points": ["done"], "synopsis": "Implemented X widget.",
        }])
        with patch.object(extractor.ollama, "_generate", return_value=response):
            entities = extractor.process_pending()

        assert entities[0].source_event_id == event.id
        conn = memory_db.connect()
        try:
            row = conn.execute(
                "SELECT session_id FROM events WHERE id = ?", (event.id,),
            ).fetchone()
        finally:
            conn.close()
        assert row["session_id"] == session.id


class TestFullPipelineAutoResolve:
    """ingest → extract → auto-resolve in a single run."""

    def test_todo_then_feature_in_separate_jobs_closes_todo(
        self, memory_db: Database,
    ) -> None:
        engine, extractor = _make_engine_and_extractor(memory_db)
        engine.start_session()

        # Job 1: an event that extracts a TODO
        engine.ingest_one("note", "need cursor pagination for list endpoints")
        todo_response = _extraction_json(todos=[{
            "title": "Add cursor-based pagination to list endpoints",
            "content": "Paginate list responses.",
            "status": "open", "priority": "medium",
            "key_points": ["offset has skew"], "synopsis": "Pagination plan.",
        }])
        with patch.object(extractor.ollama, "_generate", return_value=todo_response):
            extractor.process_pending()

        # Job 2: a later event extracts the resolving feature
        engine.ingest_one(
            "note", "shipped cursor-based pagination across list endpoints",
        )
        feature_response = _extraction_json(features=[{
            "title": "Cursor-based pagination on list endpoints",
            "content": "Cursor tokens wired into routes.",
            "key_points": ["returns next_cursor"],
            "synopsis": "Pagination shipped.",
        }])
        with patch.object(extractor.ollama, "_generate", return_value=feature_response):
            extractor.process_pending()

        conn = memory_db.connect()
        try:
            todos = conn.execute(
                "SELECT title, status FROM entities "
                "WHERE project_id = ? AND type = 'todo'",
                (engine.project_id,),
            ).fetchall()
        finally:
            conn.close()
        assert len(todos) == 1
        assert todos[0]["status"] == "done"

    def test_sweep_closes_todo_created_after_driver(
        self, memory_db: Database,
    ) -> None:
        """If a TODO is extracted after its driver feature, the live hook
        misses it. The sweep must catch it on the next run.
        """
        engine, extractor = _make_engine_and_extractor(memory_db)
        engine.start_session()

        # Job 1: feature extracted first
        engine.ingest_one("note", "implemented JWT authentication middleware")
        feature_response = _extraction_json(features=[{
            "title": "JWT authentication middleware",
            "content": "Added JWT validation middleware.",
            "key_points": ["uses RS256"],
            "synopsis": "JWT auth shipped.",
        }])
        with patch.object(extractor.ollama, "_generate", return_value=feature_response):
            extractor.process_pending()

        # Job 2: a belated TODO for the same work — live hook won't catch it
        engine.ingest_one("note", "need to add JWT authentication middleware")
        belated_response = _extraction_json(todos=[{
            "title": "Add JWT authentication middleware",
            "content": "Auth middleware required.",
            "status": "open", "priority": "medium",
            "key_points": ["protect API routes"],
            "synopsis": "Auth backlog item.",
        }])
        with patch.object(extractor.ollama, "_generate", return_value=belated_response):
            extractor.process_pending()

        # Live hook missed — TODO still open
        conn = memory_db.connect()
        try:
            row = conn.execute(
                "SELECT status FROM entities "
                "WHERE project_id = ? AND type = 'todo'",
                (engine.project_id,),
            ).fetchone()
        finally:
            conn.close()
        assert row["status"] == "open"

        # Sweep catches it
        records = extractor.sweep_resolutions(engine.project_id)
        assert len(records) == 1
        assert records[0]["status"] == "done"

        conn = memory_db.connect()
        try:
            row = conn.execute(
                "SELECT status FROM entities "
                "WHERE project_id = ? AND type = 'todo'",
                (engine.project_id,),
            ).fetchone()
        finally:
            conn.close()
        assert row["status"] == "done"

    def test_extraction_failure_does_not_corrupt_state(
        self, memory_db: Database,
    ) -> None:
        engine, extractor = _make_engine_and_extractor(memory_db)
        engine.start_session()
        engine.ingest_one("note", "something significant happened")

        # Malformed response — _parse_extraction returns {}, job completes empty
        with patch.object(
            extractor.ollama, "_generate", return_value="not json at all",
        ):
            entities = extractor.process_pending()

        assert entities == []
        conn = memory_db.connect()
        try:
            entity_count = conn.execute(
                "SELECT COUNT(*) c FROM entities WHERE project_id = ?",
                (engine.project_id,),
            ).fetchone()["c"]
        finally:
            conn.close()
        assert entity_count == 0


class TestBriefingAfterPipeline:
    """Briefing should reflect what the pipeline produced."""

    def test_briefing_includes_extracted_todo(
        self, memory_db: Database,
    ) -> None:
        engine, extractor = _make_engine_and_extractor(memory_db)
        engine.start_session()
        engine.ingest_one("note", "need stricter input validation")
        response = _extraction_json(todos=[{
            "title": "Tighten input validation on public endpoints",
            "content": "Add validation layer.", "status": "open",
            "priority": "high",
            "key_points": ["whitelist fields"],
            "synopsis": "Input validation plan.",
        }])
        with patch.object(extractor.ollama, "_generate", return_value=response):
            extractor.process_pending()

        briefing = engine.get_briefing()
        assert "Tighten input validation" in briefing["content"]
        assert briefing["observations_loaded"] >= 1
