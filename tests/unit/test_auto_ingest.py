"""Tests for auto-ingestion pattern detection and direct entity creation."""

from __future__ import annotations

from callmem.core.auto_ingest import detect_ingestable_content


class TestAutoDetect:
    def test_detects_decision(self) -> None:
        text = "Let's go with FastAPI for the REST API. It has better async support."
        results = detect_ingestable_content(text)
        assert any(r.type == "decision" for r in results)

    def test_detects_discovery(self) -> None:
        text = "Turns out the issue was a missing semicolon in the config file."
        results = detect_ingestable_content(text)
        assert any(r.type == "discovery" for r in results)

    def test_detects_failure(self) -> None:
        text = "This didn't work because the API returns 401 without auth."
        results = detect_ingestable_content(text)
        assert any(r.type == "failure" for r in results)

    def test_detects_todo(self) -> None:
        text = "We need to add integration tests for the new endpoint."
        results = detect_ingestable_content(text)
        assert any(r.type == "todo" for r in results)

    def test_no_false_positives_on_plain_text(self) -> None:
        text = "The weather is nice today. Let's go for a walk."
        results = detect_ingestable_content(text)
        assert results == []

    def test_multiple_detections_in_one_response(self) -> None:
        text = (
            "Let's go with PostgreSQL. Turns out SQLite doesn't handle "
            "concurrent writes well. We need to add connection pooling."
        )
        results = detect_ingestable_content(text)
        types = {r.type for r in results}
        assert "decision" in types
        assert "discovery" in types
        assert "todo" in types

    def test_empty_text_returns_empty(self) -> None:
        assert detect_ingestable_content("") == []
        assert detect_ingestable_content("short") == []

    def test_deduplicates_same_type_and_content(self) -> None:
        text = "Let's go with FastAPI. Let's go with FastAPI."
        results = detect_ingestable_content(text)
        decisions = [r for r in results if r.type == "decision"]
        assert len(decisions) == 1


class TestDirectEntityCreation:
    def test_decision_event_creates_entity_immediately(
        self, engine,
    ) -> None:
        engine.start_session()
        engine.ingest_one(
            type="decision",
            content="Use PostgreSQL instead of SQLite for production.",
        )
        entities = engine.get_entities(type="decision")
        assert len(entities) >= 1
        assert any(
            "PostgreSQL" in e["title"] for e in entities
        )

    def test_todo_event_creates_entity_immediately(
        self, engine,
    ) -> None:
        engine.start_session()
        engine.ingest_one(
            type="todo",
            content="Add integration tests for the auth middleware.",
        )
        entities = engine.get_entities(type="todo")
        assert len(entities) >= 1
        assert entities[0]["status"] == "open"

    def test_note_event_does_not_create_entity(self, engine) -> None:
        engine.start_session()
        engine.ingest_one(
            type="note",
            content="This is a note that should not become an entity.",
        )
        entities = engine.get_entities()
        note_entities = [e for e in entities if e["type"] == "note"]
        assert len(note_entities) == 0
