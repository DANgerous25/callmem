"""Tests for engine.reopen_entity and the mem_reopen MCP tool.

Live-forensics defect: an agent needed to reopen four wrongly-closed
entities, found no reopen operation, hand-rolled SQLite, and left one
entity in a half-state (status='done', resolved_at=NULL). mem_reopen is
the symmetric inverse of mem_resolve so raw SQL is never needed.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from callmem.core.engine import MemoryEngine
from callmem.mcp.tools import (
    _HANDLERS,
    _WRITE_TOOLS,
    TOOL_DEFINITIONS,
    handle_reopen,
)
from callmem.models.config import Config
from callmem.models.entities import Entity

if TYPE_CHECKING:
    from callmem.core.database import Database


def _make_engine(memory_db: Database) -> MemoryEngine:
    return MemoryEngine(memory_db, Config())


def _parse(result: list) -> dict:
    return json.loads(result[0].text)


class TestMemReopenToolRegistration:
    def test_registered_in_tool_definitions(self) -> None:
        names = {t["name"] for t in TOOL_DEFINITIONS}
        assert "mem_reopen" in names

    def test_registered_in_handlers(self) -> None:
        assert _HANDLERS["mem_reopen"] is handle_reopen

    def test_registered_as_write_tool(self) -> None:
        assert "mem_reopen" in _WRITE_TOOLS


class TestMemReopenTool:
    def test_happy_path_todo_reopens_to_open(self, memory_db: Database) -> None:
        engine = _make_engine(memory_db)
        entity = Entity(
            project_id=engine.project_id, type="todo", status="done",
            title="wrongly closed", content="c",
        )
        engine.repo.create_entity(entity)
        engine.repo.mark_resolved(entity.id, "done")

        data = _parse(handle_reopen(engine, {"entity_ids": [entity.id]}))
        assert data["count"] == 1
        entry = data["results"][0]
        assert entry["id"] == entity.id
        assert entry["old_status"] == "done"
        assert entry["new_status"] == "open"

    def test_accepts_short_ids(self, memory_db: Database) -> None:
        engine = _make_engine(memory_db)
        entity = Entity(
            project_id=engine.project_id, type="todo", status="done",
            title="wrongly closed", content="c",
        )
        engine.repo.create_entity(entity)

        data = _parse(handle_reopen(engine, {"entity_ids": [entity.id[-8:]]}))
        assert data["results"][0]["id"] == entity.id
        assert data["results"][0]["new_status"] == "open"

    def test_failure_reopens_to_unresolved(self, memory_db: Database) -> None:
        engine = _make_engine(memory_db)
        entity = Entity(
            project_id=engine.project_id, type="failure", status="resolved",
            title="root cause turned out wrong", content="c",
        )
        engine.repo.create_entity(entity)

        data = _parse(handle_reopen(engine, {"entity_ids": [entity.id]}))
        assert data["results"][0]["new_status"] == "unresolved"

    def test_repairs_half_state(self, memory_db: Database) -> None:
        """The exact incident state: status='done' with resolved_at never
        set. Must reopen cleanly, not error or leave it half-fixed."""
        engine = _make_engine(memory_db)
        entity = Entity(
            project_id=engine.project_id, type="todo", status="done",
            title="half-closed", content="c", resolved_at=None,
        )
        engine.repo.create_entity(entity)

        data = _parse(handle_reopen(engine, {"entity_ids": [entity.id]}))
        entry = data["results"][0]
        assert entry["new_status"] == "open"
        row = engine.repo.get_entity(entity.id)
        assert row["status"] == "open"
        assert row["resolved_at"] is None

    def test_unchanged_marker_on_no_op(self, memory_db: Database) -> None:
        engine = _make_engine(memory_db)
        entity = Entity(
            project_id=engine.project_id, type="todo", status="open",
            title="never closed", content="c",
        )
        engine.repo.create_entity(entity)

        data = _parse(handle_reopen(engine, {"entity_ids": [entity.id]}))
        entry = data["results"][0]
        assert entry["unchanged"] is True
        assert entry["old_status"] == "open"
        assert entry["new_status"] == "open"

    def test_per_entity_errors_are_not_batch_fatal(
        self, memory_db: Database,
    ) -> None:
        engine = _make_engine(memory_db)
        good = Entity(
            project_id=engine.project_id, type="todo", status="done",
            title="good", content="c",
        )
        engine.repo.create_entity(good)

        data = _parse(handle_reopen(
            engine, {"entity_ids": [good.id, "totally-unknown"]},
        ))
        assert data["count"] == 2
        ok_entry = next(r for r in data["results"] if r.get("id") == good.id)
        assert ok_entry["new_status"] == "open"
        bad_entry = next(r for r in data["results"] if r.get("id") != good.id)
        assert "error" in bad_entry

    def test_note_is_recorded_in_metadata(self, memory_db: Database) -> None:
        engine = _make_engine(memory_db)
        entity = Entity(
            project_id=engine.project_id, type="todo", status="done",
            title="t", content="c",
        )
        engine.repo.create_entity(entity)

        handle_reopen(
            engine, {"entity_ids": [entity.id], "note": "reopened by mistake"},
        )
        row = engine.repo.get_entity(entity.id)
        assert json.loads(row["metadata"])["resolution_note"] == "reopened by mistake"

    def test_removes_resolution_note_non_destructively(
        self, memory_db: Database,
    ) -> None:
        engine = _make_engine(memory_db)
        entity = Entity(
            project_id=engine.project_id, type="todo", status="open",
            title="t", content="c",
            metadata={"source": "auto-extraction"},
        )
        engine.repo.create_entity(entity)
        engine.repo.mark_resolved(entity.id, "done", note="shipped in v2")

        handle_reopen(engine, {"entity_ids": [entity.id]})
        row = engine.repo.get_entity(entity.id)
        metadata = json.loads(row["metadata"])
        assert "resolution_note" not in metadata
        assert metadata["source"] == "auto-extraction"

    def test_leaves_stale_and_pinned_untouched(self, memory_db: Database) -> None:
        engine = _make_engine(memory_db)
        entity = Entity(
            project_id=engine.project_id, type="todo", status="done",
            title="closed but stale/pinned", content="c",
            pinned=True,
        )
        engine.repo.create_entity(entity)
        engine.repo.mark_stale(entity.id, reason="manual")

        handle_reopen(engine, {"entity_ids": [entity.id]})
        row = engine.repo.get_entity(entity.id)
        assert row["stale"] == 1
        assert row["pinned"] == 1

    def test_resolved_at_included_in_result(self, memory_db: Database) -> None:
        engine = _make_engine(memory_db)
        entity = Entity(
            project_id=engine.project_id, type="todo", status="done",
            title="t", content="c",
        )
        engine.repo.create_entity(entity)

        data = _parse(handle_reopen(engine, {"entity_ids": [entity.id]}))
        entry = data["results"][0]
        assert "resolved_at" in entry
        assert entry["resolved_at"] is None

    def test_stale_entity_flagged_in_result(self, memory_db: Database) -> None:
        """Reopening a stale+closed entity restores status but the
        entity stays suppressed from briefings until mem_mark_current is
        also called. The result must disclose that, not just report
        success."""
        engine = _make_engine(memory_db)
        entity = Entity(
            project_id=engine.project_id, type="todo", status="done",
            title="closed and stale", content="c",
        )
        engine.repo.create_entity(entity)
        engine.repo.mark_stale(entity.id, reason="manual")

        data = _parse(handle_reopen(engine, {"entity_ids": [entity.id]}))
        entry = data["results"][0]
        assert entry["stale"] is True

    def test_non_stale_entity_omits_stale_key(self, memory_db: Database) -> None:
        engine = _make_engine(memory_db)
        entity = Entity(
            project_id=engine.project_id, type="todo", status="done",
            title="closed, not stale", content="c",
        )
        engine.repo.create_entity(entity)

        data = _parse(handle_reopen(engine, {"entity_ids": [entity.id]}))
        entry = data["results"][0]
        assert "stale" not in entry

    def test_rejects_decision_type_per_entity(self, memory_db: Database) -> None:
        engine = _make_engine(memory_db)
        entity = Entity(
            project_id=engine.project_id, type="decision", status="done",
            title="use postgres", content="c",
        )
        engine.repo.create_entity(entity)

        data = _parse(handle_reopen(engine, {"entity_ids": [entity.id]}))
        entry = data["results"][0]
        assert "error" in entry
        row = engine.repo.get_entity(entity.id)
        assert row["status"] == "done"

    def test_rejects_fact_type_per_entity(self, memory_db: Database) -> None:
        engine = _make_engine(memory_db)
        entity = Entity(
            project_id=engine.project_id, type="fact",
            title="the API rate limit is 100rpm", content="c",
        )
        engine.repo.create_entity(entity)

        data = _parse(handle_reopen(engine, {"entity_ids": [entity.id]}))
        entry = data["results"][0]
        assert "error" in entry

    def test_unsupported_type_is_not_batch_fatal(self, memory_db: Database) -> None:
        engine = _make_engine(memory_db)
        good = Entity(
            project_id=engine.project_id, type="todo", status="done",
            title="good", content="c",
        )
        bad = Entity(
            project_id=engine.project_id, type="decision", status="done",
            title="bad", content="c",
        )
        engine.repo.create_entity(good)
        engine.repo.create_entity(bad)

        data = _parse(handle_reopen(
            engine, {"entity_ids": [good.id, bad.id]},
        ))
        assert data["count"] == 2
        ok_entry = next(r for r in data["results"] if r.get("id") == good.id)
        assert ok_entry["new_status"] == "open"
        bad_entry = next(r for r in data["results"] if r.get("id") == bad.id)
        assert "error" in bad_entry

    def test_never_closed_todo_is_unchanged(self, memory_db: Database) -> None:
        engine = _make_engine(memory_db)
        entity = Entity(
            project_id=engine.project_id, type="todo",
            title="never touched", content="c",
        )
        engine.repo.create_entity(entity)
        assert entity.status is None

        data = _parse(handle_reopen(engine, {"entity_ids": [entity.id]}))
        entry = data["results"][0]
        assert entry["unchanged"] is True
        assert entry["old_status"] is None
        row = engine.repo.get_entity(entity.id)
        assert row["status"] is None
