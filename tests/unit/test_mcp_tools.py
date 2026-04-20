"""Tests for MCP tool handlers."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from callmem.core.engine import MemoryEngine
from callmem.mcp.tools import (
    TOOL_DEFINITIONS,
    handle_check_context,
    handle_compress_context,
    handle_get_entities,
    handle_get_tasks,
    handle_ingest,
    handle_pin,
    handle_search,
    handle_session_end,
    handle_session_start,
)
from callmem.models.config import Config

if TYPE_CHECKING:
    from callmem.core.database import Database


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
            "mem_search_by_file", "mem_file_context",
            "mem_check_context", "mem_compress_context",
            "mem_vault_review", "mem_mark_stale", "mem_mark_current",
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
        from callmem.models.entities import Entity
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


class TestFileContextTool:
    def _seed_entity_with_file(
        self, memory_db: Database, engine: MemoryEngine, path: str,
    ) -> None:
        from callmem.models.entities import Entity

        entity = Entity(
            project_id=engine.project_id,
            source_event_id=None,
            type="feature",
            title="Wrote JWT middleware",
            content="Wrote JWT middleware",
        )
        row = entity.to_row()
        conn = memory_db.connect()
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
                (row["id"], path),
            )
            conn.commit()
        finally:
            conn.close()

    def test_tool_is_registered(self) -> None:
        from callmem.mcp.tools import _HANDLERS

        assert "mem_file_context" in _HANDLERS

    def test_missing_path_returns_error(self, memory_db: Database) -> None:
        from callmem.mcp.tools import handle_file_context

        engine = _make_engine(memory_db)
        result = handle_file_context(engine, {})
        data = _parse_result(result)
        assert "error" in data

    def test_unknown_file_not_an_error(self, memory_db: Database) -> None:
        from callmem.mcp.tools import handle_file_context

        engine = _make_engine(memory_db)
        result = handle_file_context(engine, {"path": "src/x.py"})
        data = _parse_result(result)
        assert data["has_observations"] is False
        assert data["observation_count"] == 0

    def test_known_file_returns_timeline(
        self, memory_db: Database,
    ) -> None:
        from callmem.mcp.tools import handle_file_context

        engine = _make_engine(memory_db)
        self._seed_entity_with_file(
            memory_db, engine, "src/auth/middleware.py",
        )
        result = handle_file_context(
            engine, {"path": "src/auth/middleware.py"},
        )
        data = _parse_result(result)
        assert data["has_observations"] is True
        assert data["observation_count"] == 1
        assert data["timeline"][0]["type"] == "feature"


class TestPin:
    def test_pin_nonexistent_entity(self, memory_db: Database) -> None:
        engine = _make_engine(memory_db)
        with pytest.raises(ValueError, match="Entity not found"):
            handle_pin(engine, {"entity_id": "nonexistent", "pinned": True})


class TestCheckContext:
    def _engine_with_limit(
        self, memory_db: Database, context_limit: int = 8000,
        enabled: bool = True, threshold: float = 0.8,
    ) -> MemoryEngine:
        config = Config(endless_mode={
            "enabled": enabled,
            "context_limit": context_limit,
            "compress_threshold": threshold,
        })
        return MemoryEngine(memory_db, config)

    def test_under_threshold_ok(self, memory_db: Database) -> None:
        engine = self._engine_with_limit(memory_db)
        result = handle_check_context(
            engine, {"message_count": 5, "estimated_tokens": 1000},
        )
        data = _parse_result(result)
        assert data["status"] == "ok"
        assert data["usage_ratio"] < 0.8
        assert data["context_limit"] == 8000

    def test_over_threshold_recommends_compression(
        self, memory_db: Database,
    ) -> None:
        engine = self._engine_with_limit(memory_db)
        result = handle_check_context(
            engine, {"message_count": 200, "estimated_tokens": 7000},
        )
        data = _parse_result(result)
        assert data["status"] == "compress_recommended"
        assert data["usage_ratio"] >= 0.8
        assert data["free_tokens_hint"] > 0
        assert "mem_compress_context" in data["action"]

    def test_disabled_returns_disabled_status(
        self, memory_db: Database,
    ) -> None:
        engine = self._engine_with_limit(memory_db, enabled=False)
        result = handle_check_context(
            engine, {"message_count": 500, "estimated_tokens": 1_000_000},
        )
        data = _parse_result(result)
        assert data["status"] == "disabled"

    def test_missing_tokens_falls_back_to_message_heuristic(
        self, memory_db: Database,
    ) -> None:
        engine = self._engine_with_limit(memory_db, context_limit=1000)
        result = handle_check_context(engine, {"message_count": 20})
        data = _parse_result(result)
        assert data["effective_tokens"] == 20 * 500
        assert data["status"] == "compress_recommended"


class TestCompressContext:
    def test_empty_summary_returns_error(self, memory_db: Database) -> None:
        engine = _make_engine(memory_db)
        result = handle_compress_context(engine, {"summary": ""})
        data = _parse_result(result)
        assert "error" in data

    def test_no_active_session_returns_error(
        self, memory_db: Database,
    ) -> None:
        engine = _make_engine(memory_db)
        result = handle_compress_context(
            engine, {"summary": "some summary"},
        )
        data = _parse_result(result)
        assert "error" in data
        assert "active session" in data["error"].lower()

    def test_persists_summary_and_returns_marker(
        self, memory_db: Database,
    ) -> None:
        engine = _make_engine(memory_db)
        handle_session_start(engine, {"agent_name": "test"})
        result = handle_compress_context(engine, {
            "summary": "Decided to use JWT for auth (messages 1-30).",
            "message_range": "messages 1-30",
        })
        data = _parse_result(result)
        assert data["status"] == "compressed"
        assert data["compression_events"] == 1
        assert "compressed" in data["marker"].lower()
        assert data["message_range"] == "messages 1-30"
