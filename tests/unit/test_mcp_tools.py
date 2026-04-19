"""Tests for MCP tool handlers."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from llm_mem.core.engine import MemoryEngine
from llm_mem.mcp.tools import (
    TOOL_DEFINITIONS,
    handle_get_entities,
    handle_get_tasks,
    handle_ingest,
    handle_pin,
    handle_search,
    handle_session_end,
    handle_session_start,
)
from llm_mem.models.config import Config

if TYPE_CHECKING:
    from llm_mem.core.database import Database


def _make_engine(memory_db: Database) -> MemoryEngine:
    return MemoryEngine(memory_db, Config())


def _parse_result(content: list) -> dict:
    return json.loads(content[0].text)


class TestToolDefinitions:
    def test_all_tools_defined(self) -> None:
        names = {t["name"] for t in TOOL_DEFINITIONS}
        expected = {
            "mem_session_start", "mem_session_end", "mem_ingest",
            "mem_search", "mem_get_briefing", "mem_get_tasks", "mem_pin",
            "mem_search_index", "mem_timeline", "mem_get_entities",
            "mem_search_by_file", "mem_vault_review",
            "mem_mark_stale", "mem_mark_current",
        }
        assert names == expected

    def test_tools_have_required_fields(self) -> None:
        for t in TOOL_DEFINITIONS:
            assert "name" in t
            assert "description" in t
            assert "inputSchema" in t


class TestSessionStart:
    def test_creates_session(self, memory_db: Database) -> None:
        engine = _make_engine(memory_db)
        result = handle_session_start(engine, {"agent_name": "test"})
        data = _parse_result(result)
        assert "session_id" in data
        assert data["briefing"] == "Session started."

    def test_with_model_name(self, memory_db: Database) -> None:
        engine = _make_engine(memory_db)
        result = handle_session_start(engine, {
            "agent_name": "opencode",
            "model_name": "glm-5",
        })
        data = _parse_result(result)
        assert "session_id" in data


class TestSessionEnd:
    def test_ends_active_session(self, memory_db: Database) -> None:
        engine = _make_engine(memory_db)
        handle_session_start(engine, {})
        result = handle_session_end(engine, {"note": "done"})
        data = _parse_result(result)
        assert data["status"] == "ended"

    def test_no_active_session(self, memory_db: Database) -> None:
        engine = _make_engine(memory_db)
        result = handle_session_end(engine, {})
        data = _parse_result(result)
        assert "error" in data


class TestIngest:
    def test_ingest_single_event(self, memory_db: Database) -> None:
        engine = _make_engine(memory_db)
        handle_session_start(engine, {})
        result = handle_ingest(engine, {
            "events": [{"type": "prompt", "content": "Hello"}],
        })
        data = _parse_result(result)
        assert data["ingested"] == 1
        assert len(data["event_ids"]) == 1

    def test_ingest_multiple_events(self, memory_db: Database) -> None:
        engine = _make_engine(memory_db)
        handle_session_start(engine, {})
        result = handle_ingest(engine, {
            "events": [
                {"type": "prompt", "content": "Fix the bug"},
                {"type": "response", "content": "Looking into it"},
            ],
        })
        data = _parse_result(result)
        assert data["ingested"] == 2

    def test_ingest_with_metadata(self, memory_db: Database) -> None:
        engine = _make_engine(memory_db)
        handle_session_start(engine, {})
        result = handle_ingest(engine, {
            "events": [{
                "type": "tool_call",
                "content": "ran pytest",
                "metadata": {"tool": "pytest"},
            }],
        })
        data = _parse_result(result)
        assert data["ingested"] == 1

    def test_ingest_empty_list(self, memory_db: Database) -> None:
        engine = _make_engine(memory_db)
        handle_session_start(engine, {})
        result = handle_ingest(engine, {"events": []})
        data = _parse_result(result)
        assert data["ingested"] == 0

    def test_ingest_auto_creates_session(self, memory_db: Database) -> None:
        engine = _make_engine(memory_db)
        result = handle_ingest(engine, {
            "events": [{"type": "note", "content": "auto session"}],
        })
        data = _parse_result(result)
        assert data["ingested"] == 1


class TestSearch:
    def test_search_finds_events(self, memory_db: Database) -> None:
        engine = _make_engine(memory_db)
        handle_session_start(engine, {})
        handle_ingest(engine, {
            "events": [{"type": "decision", "content": "Use Redis for caching"}],
        })
        result = handle_search(engine, {"query": "Redis caching"})
        data = _parse_result(result)
        assert len(data["results"]) >= 1
        assert "Redis" in data["results"][0]["content"]

    def test_search_empty_results(self, memory_db: Database) -> None:
        engine = _make_engine(memory_db)
        handle_session_start(engine, {})
        result = handle_search(engine, {"query": "nonexistent"})
        data = _parse_result(result)
        assert data["results"] == []


class TestGetTasks:
    def test_returns_empty_when_no_tasks(self, memory_db: Database) -> None:
        engine = _make_engine(memory_db)
        result = handle_get_tasks(engine, {"status": "open"})
        data = _parse_result(result)
        assert data["tasks"] == []

    def test_default_status_is_open(self, memory_db: Database) -> None:
        engine = _make_engine(memory_db)
        result = handle_get_tasks(engine, {})
        data = _parse_result(result)
        assert data["tasks"] == []


class TestGetEntities:
    def _seed_entity(self, memory_db: Database) -> str:
        engine = _make_engine(memory_db)
        handle_session_start(engine, {})
        handle_ingest(engine, {
            "events": [{
                "type": "decision",
                "content": "Pick SQLite for local storage",
            }],
        })
        from llm_mem.models.entities import Entity
        entity = Entity(
            project_id=engine.project_id,
            type="decision",
            title="Use SQLite",
            content="Pick SQLite for local storage",
        )
        conn = memory_db.connect()
        try:
            row = entity.to_row()
            conn.execute(
                "INSERT INTO entities "
                "(id, project_id, source_event_id, type, title, content, "
                "key_points, synopsis, status, priority, pinned, "
                "created_at, updated_at, resolved_at, metadata, archived_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    row["id"], row["project_id"], row["source_event_id"],
                    row["type"], row["title"], row["content"],
                    row["key_points"], row["synopsis"],
                    row["status"], row["priority"], row["pinned"],
                    row["created_at"], row["updated_at"],
                    row["resolved_at"], row["metadata"], row["archived_at"],
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return entity.id

    def test_get_by_full_id(self, memory_db: Database) -> None:
        eid = self._seed_entity(memory_db)
        engine = _make_engine(memory_db)
        result = handle_get_entities(engine, {"ids": [eid]})
        data = _parse_result(result)
        assert data["count"] == 1
        assert data["entities"][0]["id"] == eid

    def test_get_by_short_suffix(self, memory_db: Database) -> None:
        eid = self._seed_entity(memory_db)
        engine = _make_engine(memory_db)
        result = handle_get_entities(engine, {"ids": [eid[-8:]]})
        data = _parse_result(result)
        assert data["count"] == 1
        assert data["entities"][0]["id"] == eid

    def test_get_strips_leading_hash(self, memory_db: Database) -> None:
        eid = self._seed_entity(memory_db)
        engine = _make_engine(memory_db)
        result = handle_get_entities(engine, {"ids": [f"#{eid[-8:]}"]})
        data = _parse_result(result)
        assert data["count"] == 1


class TestPin:
    def test_pin_nonexistent_entity(self, memory_db: Database) -> None:
        engine = _make_engine(memory_db)
        with pytest.raises(ValueError, match="Entity not found"):
            handle_pin(engine, {"entity_id": "nonexistent", "pinned": True})
