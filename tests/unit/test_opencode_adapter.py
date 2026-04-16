"""Tests for the OpenCode SSE adapter."""

from __future__ import annotations

from typing import TYPE_CHECKING

from llm_mem.adapters.opencode import OpenCodeAdapter
from llm_mem.models.config import Config

if TYPE_CHECKING:
    from llm_mem.core.database import Database
    pass


def _make_engine(memory_db: Database) -> tuple:
    from llm_mem.core.engine import MemoryEngine

    config = Config(sensitive_data={"enabled": False, "llm_scan": False})
    engine = MemoryEngine(memory_db, config)
    return engine


class TestEventMapping:
    def test_user_message_mapped_to_prompt(
        self, memory_db: Database
    ) -> None:
        engine = _make_engine(memory_db)
        adapter = OpenCodeAdapter(engine)

        event = {
            "type": "message.created",
            "data": {"role": "user", "content": "Fix the bug in auth.py"},
        }
        result = adapter.process_event(event)
        assert result is not None
        assert result.type == "prompt"
        assert "Fix the bug" in result.content

    def test_assistant_message_mapped_to_response(
        self, memory_db: Database
    ) -> None:
        engine = _make_engine(memory_db)
        adapter = OpenCodeAdapter(engine)

        event = {
            "type": "message.created",
            "data": {"role": "assistant", "content": "I fixed the bug"},
        }
        result = adapter.process_event(event)
        assert result is not None
        assert result.type == "response"

    def test_tool_invocation_mapped(self, memory_db: Database) -> None:
        engine = _make_engine(memory_db)
        adapter = OpenCodeAdapter(engine)

        event = {
            "type": "tool.invoked",
            "data": {
                "tool": "write_file",
                "args": {"path": "src/api.py"},
            },
        }
        result = adapter.process_event(event)
        assert result is not None
        assert result.type == "tool_call"
        assert "write_file" in result.content

    def test_file_change_mapped(self, memory_db: Database) -> None:
        engine = _make_engine(memory_db)
        adapter = OpenCodeAdapter(engine)

        event = {
            "type": "file.changed",
            "data": {"path": "src/api.py", "change": "modified"},
        }
        result = adapter.process_event(event)
        assert result is not None
        assert result.type == "file_change"
        assert "src/api.py" in result.content

    def test_session_created_starts_session(
        self, memory_db: Database
    ) -> None:
        engine = _make_engine(memory_db)
        adapter = OpenCodeAdapter(engine)

        event = {"type": "session.created", "data": {}}
        result = adapter.process_event(event)
        assert result is None
        assert engine.get_active_session() is not None

    def test_session_completed_ends_session(
        self, memory_db: Database
    ) -> None:
        engine = _make_engine(memory_db)
        adapter = OpenCodeAdapter(engine)
        engine.start_session()

        event = {"type": "session.completed", "data": {}}
        result = adapter.process_event(event)
        assert result is None
        assert engine.get_active_session() is None

    def test_unknown_event_returns_none(
        self, memory_db: Database
    ) -> None:
        engine = _make_engine(memory_db)
        adapter = OpenCodeAdapter(engine)

        event = {"type": "unknown.event", "data": {}}
        result = adapter.process_event(event)
        assert result is None

    def test_empty_message_returns_none(
        self, memory_db: Database
    ) -> None:
        engine = _make_engine(memory_db)
        adapter = OpenCodeAdapter(engine)

        event = {
            "type": "message.created",
            "data": {"role": "user", "content": ""},
        }
        result = adapter.process_event(event)
        assert result is None


class TestIngestFromEvent:
    def test_adapter_ingests_prompt(
        self, memory_db: Database
    ) -> None:
        engine = _make_engine(memory_db)
        engine.start_session()
        adapter = OpenCodeAdapter(engine)

        event = {
            "type": "message.created",
            "data": {"role": "user", "content": "Refactor the database layer"},
        }
        adapter._handle_event(event)

        events = engine.get_events(type="prompt")
        assert len(events) == 1
        assert "Refactor" in events[0].content

    def test_adapter_ingests_tool_call(
        self, memory_db: Database
    ) -> None:
        engine = _make_engine(memory_db)
        engine.start_session()
        adapter = OpenCodeAdapter(engine)

        event = {
            "type": "tool.invoked",
            "data": {"tool": "read_file", "args": {"path": "config.py"}},
        }
        adapter._handle_event(event)

        events = engine.get_events(type="tool_call")
        assert len(events) == 1
