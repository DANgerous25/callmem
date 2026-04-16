"""Tests for data models."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from llm_mem.models.events import Event, EventInput
from llm_mem.models.sessions import Session
from llm_mem.models.entities import Entity
from llm_mem.models.projects import Project
from llm_mem.models.summaries import Summary
from llm_mem.models.edges import MemoryEdge


class TestEvent:
    def test_creation_with_defaults(self) -> None:
        event = Event(session_id="sess1", project_id="proj1", type="prompt", content="Hello")
        assert event.id is not None
        assert len(event.id) == 26  # ULID length
        assert event.timestamp is not None
        assert event.type == "prompt"
        assert event.archived_at is None

    def test_round_trip(self) -> None:
        event = Event(
            session_id="sess1",
            project_id="proj1",
            type="tool_call",
            content="ran tests",
            metadata={"tool_name": "pytest"},
        )
        row = event.to_row()
        reconstructed = Event.from_row(row)
        assert reconstructed.id == event.id
        assert reconstructed.type == event.type
        assert reconstructed.metadata == event.metadata

    def test_invalid_type_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Event(session_id="s", project_id="p", type="invalid", content="x")


class TestEventInput:
    def test_minimal_input(self) -> None:
        inp = EventInput(type="prompt", content="test")
        assert inp.type == "prompt"
        assert inp.metadata is None


class TestSession:
    def test_creation(self) -> None:
        session = Session(project_id="proj1")
        assert session.status == "active"
        assert session.event_count == 0

    def test_round_trip(self) -> None:
        session = Session(project_id="proj1", agent_name="opencode", model_name="claude")
        row = session.to_row()
        reconstructed = Session.from_row(row)
        assert reconstructed.agent_name == "opencode"


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

    def test_pinned_serialization(self) -> None:
        entity = Entity(
            project_id="proj1", type="fact", title="Test", content="Test",
            pinned=True,
        )
        row = entity.to_row()
        assert row["pinned"] == 1
        reconstructed = Entity.from_row(row)
        assert reconstructed.pinned is True

    def test_invalid_type_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Entity(project_id="p", type="invalid", title="x", content="x")


class TestProject:
    def test_creation(self) -> None:
        project = Project(name="my-project", root_path="/home/user/project")
        assert project.id is not None
        assert project.name == "my-project"


class TestSummary:
    def test_creation(self) -> None:
        summary = Summary(
            project_id="proj1", level="session",
            content="Implemented pagination", event_count=42,
        )
        assert summary.session_id is None
        assert summary.token_count is None


class TestMemoryEdge:
    def test_creation(self) -> None:
        edge = MemoryEdge(
            source_id="ent1", source_type="entity",
            target_id="ent2", target_type="entity",
            relation="relates_to",
        )
        assert edge.weight == 1.0
