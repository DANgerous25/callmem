"""Tests for engine.resolve_entity_id / resolve_entity and the mem_resolve
MCP tool.

Live-forensics defect: an agent tried to close five TODOs, mem_mark_stale
rejected the briefing's short ID, and there was no "done" verb at all --
the agent fell back to mem_mark_stale(reason="outdated"), leaving entities
open+stale=1 (invisible but unresolved). mem_resolve is the closing verb;
resolve_entity_id is the shared, project-scoped, collision-safe short-ID
resolver every entity-id-taking tool now uses.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from callmem.core.engine import (
    AmbiguousEntityIdError,
    EntityNotFoundError,
    MemoryEngine,
)
from callmem.core.repository import Repository
from callmem.mcp.tools import (
    _HANDLERS,
    _WRITE_TOOLS,
    TOOL_DEFINITIONS,
    handle_resolve,
)
from callmem.models.config import Config
from callmem.models.entities import Entity
from callmem.models.projects import Project

if TYPE_CHECKING:
    from callmem.core.database import Database


def _make_engine(memory_db: Database) -> MemoryEngine:
    return MemoryEngine(memory_db, Config())


def _parse(result: list) -> dict:
    return json.loads(result[0].text)


class TestResolveEntityId:
    def test_resolves_full_id(self, memory_db: Database) -> None:
        engine = _make_engine(memory_db)
        entity = Entity(
            project_id=engine.project_id, type="todo",
            title="t", content="c",
        )
        engine.repo.create_entity(entity)
        assert engine.resolve_entity_id(entity.id) == entity.id

    def test_resolves_short_suffix(self, memory_db: Database) -> None:
        engine = _make_engine(memory_db)
        entity = Entity(
            project_id=engine.project_id, type="todo",
            title="t", content="c",
        )
        engine.repo.create_entity(entity)
        assert engine.resolve_entity_id(entity.id[-8:]) == entity.id

    def test_strips_leading_hash(self, memory_db: Database) -> None:
        engine = _make_engine(memory_db)
        entity = Entity(
            project_id=engine.project_id, type="todo",
            title="t", content="c",
        )
        engine.repo.create_entity(entity)
        assert engine.resolve_entity_id(f"#{entity.id[-8:]}") == entity.id

    def test_unknown_id_raises_naming_the_id(self, memory_db: Database) -> None:
        engine = _make_engine(memory_db)
        with pytest.raises(EntityNotFoundError, match="ZZZZZZZZ"):
            engine.resolve_entity_id("ZZZZZZZZ")

    def test_ambiguous_short_id_lists_full_ids(self, memory_db: Database) -> None:
        engine = _make_engine(memory_db)
        e1 = Entity(
            id="AAAAAAAAAAAAAAAAAA01234567",
            project_id=engine.project_id, type="todo",
            title="one", content="c",
        )
        e2 = Entity(
            id="BBBBBBBBBBBBBBBBBB01234567",
            project_id=engine.project_id, type="todo",
            title="two", content="c",
        )
        engine.repo.create_entity(e1)
        engine.repo.create_entity(e2)

        with pytest.raises(AmbiguousEntityIdError) as exc_info:
            engine.resolve_entity_id("01234567")
        assert e1.id in str(exc_info.value)
        assert e2.id in str(exc_info.value)

    def test_full_id_from_other_project_not_found(self, memory_db: Database) -> None:
        repo = Repository(memory_db)
        other_project = Project(name="other")
        repo.create_project(other_project)
        other_entity = Entity(
            project_id=other_project.id, type="todo",
            title="t", content="c",
        )
        repo.create_entity(other_entity)

        engine = _make_engine(memory_db)
        with pytest.raises(EntityNotFoundError):
            engine.resolve_entity_id(other_entity.id)


class TestMemResolveToolRegistration:
    def test_registered_in_tool_definitions(self) -> None:
        names = {t["name"] for t in TOOL_DEFINITIONS}
        assert "mem_resolve" in names

    def test_registered_in_handlers(self) -> None:
        assert _HANDLERS["mem_resolve"] is handle_resolve

    def test_registered_as_write_tool(self) -> None:
        assert "mem_resolve" in _WRITE_TOOLS


class TestMemResolveTool:
    def test_happy_path_defaults_to_done(self, memory_db: Database) -> None:
        engine = _make_engine(memory_db)
        entity = Entity(
            project_id=engine.project_id, type="todo", status="open",
            title="ship it", content="c",
        )
        engine.repo.create_entity(entity)

        data = _parse(handle_resolve(engine, {"entity_ids": [entity.id]}))
        assert data["count"] == 1
        entry = data["results"][0]
        assert entry["id"] == entity.id
        assert entry["old_status"] == "open"
        assert entry["new_status"] == "done"
        assert entry["resolved_at"]

    def test_accepts_short_ids(self, memory_db: Database) -> None:
        engine = _make_engine(memory_db)
        entity = Entity(
            project_id=engine.project_id, type="todo", status="open",
            title="ship it", content="c",
        )
        engine.repo.create_entity(entity)

        data = _parse(handle_resolve(engine, {"entity_ids": [entity.id[-8:]]}))
        assert data["results"][0]["id"] == entity.id
        assert data["results"][0]["new_status"] == "done"

    def test_clears_stale_flag(self, memory_db: Database) -> None:
        engine = _make_engine(memory_db)
        entity = Entity(
            project_id=engine.project_id, type="todo", status="open",
            title="wrong-verb'd todo", content="c",
        )
        engine.repo.create_entity(entity)
        engine.mark_stale(entity.id, reason="outdated")
        assert engine.repo.get_entity(entity.id)["stale"] == 1

        handle_resolve(engine, {"entity_ids": [entity.id]})

        row = engine.repo.get_entity(entity.id)
        assert row["stale"] == 0
        assert row["status"] == "done"

    def test_unchanged_marker_on_no_op(self, memory_db: Database) -> None:
        engine = _make_engine(memory_db)
        entity = Entity(
            project_id=engine.project_id, type="todo", status="done",
            title="already done", content="c",
        )
        engine.repo.create_entity(entity)

        data = _parse(handle_resolve(engine, {"entity_ids": [entity.id]}))
        entry = data["results"][0]
        assert entry["unchanged"] is True
        assert entry["old_status"] == "done"
        assert entry["new_status"] == "done"

    def test_allows_resolved_status(self, memory_db: Database) -> None:
        engine = _make_engine(memory_db)
        entity = Entity(
            project_id=engine.project_id, type="failure",
            status="unresolved", title="f", content="c",
        )
        engine.repo.create_entity(entity)

        data = _parse(handle_resolve(
            engine, {"entity_ids": [entity.id], "status": "resolved"},
        ))
        assert data["results"][0]["new_status"] == "resolved"

    def test_allows_cancelled_status(self, memory_db: Database) -> None:
        engine = _make_engine(memory_db)
        entity = Entity(
            project_id=engine.project_id, type="todo", status="open",
            title="no longer needed", content="c",
        )
        engine.repo.create_entity(entity)

        data = _parse(handle_resolve(
            engine, {"entity_ids": [entity.id], "status": "cancelled"},
        ))
        assert data["results"][0]["new_status"] == "cancelled"

    def test_rejects_invalid_status(self, memory_db: Database) -> None:
        engine = _make_engine(memory_db)
        data = _parse(handle_resolve(
            engine, {"entity_ids": ["whatever"], "status": "archived"},
        ))
        assert "error" in data

    def test_per_entity_errors_are_not_batch_fatal(
        self, memory_db: Database,
    ) -> None:
        engine = _make_engine(memory_db)
        good = Entity(
            project_id=engine.project_id, type="todo", status="open",
            title="good", content="c",
        )
        engine.repo.create_entity(good)

        data = _parse(handle_resolve(
            engine, {"entity_ids": [good.id, "totally-unknown"]},
        ))
        assert data["count"] == 2
        ok_entry = next(r for r in data["results"] if r.get("id") == good.id)
        assert ok_entry["new_status"] == "done"
        bad_entry = next(r for r in data["results"] if r.get("id") != good.id)
        assert "error" in bad_entry

    def test_note_is_recorded_in_metadata(self, memory_db: Database) -> None:
        engine = _make_engine(memory_db)
        entity = Entity(
            project_id=engine.project_id, type="todo", status="open",
            title="t", content="c",
        )
        engine.repo.create_entity(entity)

        handle_resolve(
            engine, {"entity_ids": [entity.id], "note": "done in v2"},
        )
        row = engine.repo.get_entity(entity.id)
        assert json.loads(row["metadata"])["resolution_note"] == "done in v2"
