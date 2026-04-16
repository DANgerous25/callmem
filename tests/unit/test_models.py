"""Tests for data models."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from llm_mem.models.edges import MemoryEdge
from llm_mem.models.entities import Entity
from llm_mem.models.events import Event, EventInput
from llm_mem.models.projects import Project
from llm_mem.models.sessions import Session
from llm_mem.models.summaries import Summary


class TestEvent:
    def test_creation_with_defaults(self) -> None:
        event = Event(
            session_id="sess1", project_id="proj1", type="prompt", content="Hello"
        )
        assert event.id is not None
        assert len(event.id) == 26
        assert event.timestamp is not None
        assert event.type == "prompt"
        assert event.token_count is None
        assert event.archived_at is None

    def test_all_event_types(self) -> None:
        for t in [
            "prompt", "response", "tool_call", "file_change",
            "decision", "todo", "failure", "discovery", "fact", "note",
        ]:
            event = Event(session_id="s", project_id="p", type=t, content="x")
            assert event.type == t

    def test_round_trip(self) -> None:
        event = Event(
            session_id="sess1",
            project_id="proj1",
            type="tool_call",
            content="ran tests",
            metadata={"tool_name": "pytest"},
            token_count=150,
        )
        row = event.to_row()
        assert isinstance(row["metadata"], str)
        assert row["token_count"] == 150
        reconstructed = Event.from_row(row)
        assert reconstructed == event

    def test_round_trip_without_metadata(self) -> None:
        event = Event(
            session_id="s", project_id="p", type="note", content="no metadata"
        )
        row = event.to_row()
        assert row["metadata"] is None
        reconstructed = Event.from_row(row)
        assert reconstructed == event

    def test_ulid_auto_generated(self) -> None:
        a = Event(session_id="s", project_id="p", type="prompt", content="a")
        b = Event(session_id="s", project_id="p", type="prompt", content="b")
        assert a.id != b.id
        assert len(a.id) == 26

    def test_timestamp_defaults_to_now(self) -> None:
        event = Event(session_id="s", project_id="p", type="prompt", content="x")
        assert event.timestamp is not None
        assert "T" in event.timestamp

    def test_invalid_type_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Event(session_id="s", project_id="p", type="invalid", content="x")

    def test_missing_required_fields(self) -> None:
        with pytest.raises(ValidationError):
            Event(session_id="s", project_id="p", type="prompt")  # type: ignore[call-arg]


class TestEventInput:
    def test_minimal_input(self) -> None:
        inp = EventInput(type="prompt", content="test")
        assert inp.type == "prompt"
        assert inp.metadata is None

    def test_with_metadata(self) -> None:
        inp = EventInput(type="response", content="ok", metadata={"tokens": 50})
        assert inp.metadata == {"tokens": 50}

    def test_invalid_type_rejected(self) -> None:
        with pytest.raises(ValidationError):
            EventInput(type="bogus", content="x")


class TestSession:
    def test_creation_with_defaults(self) -> None:
        session = Session(project_id="proj1")
        assert session.id is not None
        assert len(session.id) == 26
        assert session.status == "active"
        assert session.event_count == 0
        assert session.started_at is not None
        assert "T" in session.started_at

    def test_round_trip(self) -> None:
        session = Session(
            project_id="proj1",
            agent_name="opencode",
            model_name="glm-5",
            metadata={"os": "linux"},
        )
        row = session.to_row()
        assert isinstance(row["metadata"], str)
        reconstructed = Session.from_row(row)
        assert reconstructed == session

    def test_all_statuses(self) -> None:
        for status in ["active", "ended", "abandoned"]:
            s = Session(project_id="p", status=status)  # type: ignore[call-arg]
            assert s.status == status

    def test_invalid_status_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Session(project_id="p", status="unknown")  # type: ignore[call-arg]

    def test_missing_project_id_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Session()  # type: ignore[call-arg]


class TestEntity:
    def test_todo_creation(self) -> None:
        entity = Entity(
            project_id="proj1",
            type="todo",
            title="Fix bug",
            content="Fix the auth bug in login.py",
            status="open",
            priority="high",
        )
        assert entity.pinned is False
        assert entity.status == "open"
        assert entity.priority == "high"
        assert entity.id is not None
        assert entity.created_at is not None
        assert "T" in entity.created_at
        assert entity.updated_at is not None

    def test_all_entity_types(self) -> None:
        for t in ["decision", "todo", "fact", "failure", "discovery"]:
            e = Entity(project_id="p", type=t, title="x", content="x")  # type: ignore[call-arg]
            assert e.type == t

    def test_all_statuses(self) -> None:
        for s in ["open", "done", "cancelled", "unresolved", "resolved"]:
            e = Entity(
                project_id="p", type="todo", title="x", content="x", status=s
            )
            assert e.status == s

    def test_all_priorities(self) -> None:
        for p in ["high", "medium", "low"]:
            e = Entity(
                project_id="p", type="todo", title="x", content="x", priority=p
            )
            assert e.priority == p

    def test_pinned_serialization(self) -> None:
        entity = Entity(
            project_id="proj1", type="fact", title="Test", content="Test", pinned=True
        )
        row = entity.to_row()
        assert row["pinned"] == 1
        reconstructed = Entity.from_row(row)
        assert reconstructed.pinned is True
        assert reconstructed == entity

    def test_round_trip_with_metadata(self) -> None:
        entity = Entity(
            project_id="p",
            type="decision",
            title="Use SQLite",
            content="Decided on SQLite for storage",
            status="open",
            metadata={"rationale": "zero-dependency"},
        )
        row = entity.to_row()
        reconstructed = Entity.from_row(row)
        assert reconstructed == entity

    def test_invalid_type_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Entity(project_id="p", type="invalid", title="x", content="x")

    def test_invalid_status_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Entity(
                project_id="p",
                type="todo",
                title="x",
                content="x",
                status="bogus",  # type: ignore[call-arg]
            )

    def test_invalid_priority_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Entity(
                project_id="p",
                type="todo",
                title="x",
                content="x",
                priority="critical",  # type: ignore[call-arg]
            )

    def test_missing_required_fields(self) -> None:
        with pytest.raises(ValidationError):
            Entity(project_id="p", type="todo", title="x")  # type: ignore[call-arg]


class TestProject:
    def test_creation_with_defaults(self) -> None:
        project = Project(name="my-project", root_path="/home/user/project")
        assert project.id is not None
        assert len(project.id) == 26
        assert project.name == "my-project"
        assert project.created_at is not None
        assert "T" in project.created_at
        assert project.updated_at is not None

    def test_round_trip(self) -> None:
        project = Project(
            name="test-proj",
            root_path="/tmp/test",
            metadata={"language": "python"},
        )
        row = project.to_row()
        assert isinstance(row["metadata"], str)
        reconstructed = Project.from_row(row)
        assert reconstructed == project

    def test_round_trip_no_optional_fields(self) -> None:
        project = Project(name="minimal")
        row = project.to_row()
        assert row["root_path"] is None
        assert row["metadata"] is None
        reconstructed = Project.from_row(row)
        assert reconstructed == project

    def test_missing_name_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Project()  # type: ignore[call-arg]


class TestSummary:
    def test_creation_with_defaults(self) -> None:
        summary = Summary(
            project_id="proj1",
            level="session",
            content="Implemented pagination",
            event_count=42,
        )
        assert summary.id is not None
        assert len(summary.id) == 26
        assert summary.session_id is None
        assert summary.token_count is None
        assert summary.created_at is not None
        assert "T" in summary.created_at

    def test_round_trip(self) -> None:
        summary = Summary(
            project_id="proj1",
            session_id="sess1",
            level="chunk",
            content="Fixed login bug",
            event_range_start="2024-01-01T00:00:00",
            event_range_end="2024-01-01T01:00:00",
            event_count=5,
            token_count=200,
            metadata={"model": "qwen3:8b"},
        )
        row = summary.to_row()
        assert isinstance(row["metadata"], str)
        reconstructed = Summary.from_row(row)
        assert reconstructed == summary

    def test_all_levels(self) -> None:
        for level in ["chunk", "session", "cross_session"]:
            s = Summary(project_id="p", level=level, content="x")
            assert s.level == level

    def test_invalid_level_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Summary(project_id="p", level="invalid", content="x")  # type: ignore[call-arg]

    def test_missing_required_fields(self) -> None:
        with pytest.raises(ValidationError):
            Summary(project_id="p", level="session")  # type: ignore[call-arg]


class TestMemoryEdge:
    def test_creation_with_defaults(self) -> None:
        edge = MemoryEdge(
            source_id="ent1",
            source_type="entity",
            target_id="ent2",
            target_type="entity",
            relation="relates_to",
        )
        assert edge.weight == 1.0
        assert edge.id is not None
        assert len(edge.id) == 26
        assert edge.created_at is not None
        assert "T" in edge.created_at

    def test_round_trip(self) -> None:
        edge = MemoryEdge(
            source_id="ent1",
            source_type="entity",
            target_id="evt1",
            target_type="event",
            relation="caused_by",
            weight=0.85,
            metadata={"confidence": 0.9},
        )
        row = edge.to_row()
        assert isinstance(row["metadata"], str)
        assert row["weight"] == 0.85
        reconstructed = MemoryEdge.from_row(row)
        assert reconstructed == edge

    def test_all_relations(self) -> None:
        for rel in ["caused_by", "relates_to", "supersedes", "resolves", "blocks"]:
            e = MemoryEdge(
                source_id="a",
                source_type="entity",
                target_id="b",
                target_type="entity",
                relation=rel,
            )
            assert e.relation == rel

    def test_invalid_relation_rejected(self) -> None:
        with pytest.raises(ValidationError):
            MemoryEdge(
                source_id="a",
                source_type="entity",
                target_id="b",
                target_type="entity",
                relation="unknown",  # type: ignore[call-arg]
            )

    def test_missing_required_fields(self) -> None:
        with pytest.raises(ValidationError):
            MemoryEdge(source_id="a", source_type="entity", relation="relates_to")  # type: ignore[call-arg]


class TestToRowJsonSerialization:
    """Verify that all metadata fields serialize to JSON strings in to_row()."""

    def test_event_metadata_serialized(self) -> None:
        event = Event(
            session_id="s",
            project_id="p",
            type="prompt",
            content="x",
            metadata={"key": "val"},
        )
        row = event.to_row()
        assert isinstance(row["metadata"], str)
        assert json.loads(row["metadata"]) == {"key": "val"}

    def test_session_metadata_serialized(self) -> None:
        session = Session(project_id="p", metadata={"a": 1})
        row = session.to_row()
        assert isinstance(row["metadata"], str)

    def test_entity_metadata_serialized(self) -> None:
        entity = Entity(
            project_id="p", type="fact", title="t", content="c", metadata={"x": "y"}
        )
        row = entity.to_row()
        assert isinstance(row["metadata"], str)

    def test_summary_metadata_serialized(self) -> None:
        summary = Summary(
            project_id="p", level="chunk", content="c", metadata={"z": 1}
        )
        row = summary.to_row()
        assert isinstance(row["metadata"], str)

    def test_project_metadata_serialized(self) -> None:
        project = Project(name="n", metadata={"lang": "py"})
        row = project.to_row()
        assert isinstance(row["metadata"], str)

    def test_edge_metadata_serialized(self) -> None:
        edge = MemoryEdge(
            source_id="a",
            source_type="entity",
            target_id="b",
            target_type="entity",
            relation="relates_to",
            metadata={"w": 0.5},
        )
        row = edge.to_row()
        assert isinstance(row["metadata"], str)

    def test_none_metadata_stays_none(self) -> None:
        event = Event(session_id="s", project_id="p", type="prompt", content="x")
        row = event.to_row()
        assert row["metadata"] is None
