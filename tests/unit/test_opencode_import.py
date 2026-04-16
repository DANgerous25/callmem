"""Tests for OpenCode session history importer."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from llm_mem.adapters.opencode_import import (
    _extract_content,
    _map_message,
    discover_session_files,
    import_session,
    import_sessions,
    read_session_file,
)

if TYPE_CHECKING:
    from pathlib import Path

    from llm_mem.core.database import Database
    from llm_mem.core.engine import MemoryEngine


@pytest.fixture
def engine_no_sensitive(memory_db: Database) -> MemoryEngine:
    from llm_mem.core.engine import MemoryEngine
    from llm_mem.models.config import Config

    config = Config(sensitive_data={"enabled": False, "llm_scan": False})
    return MemoryEngine(memory_db, config)


class TestDiscoverSessionFiles:
    def test_empty_dir(self, tmp_path: Path) -> None:
        assert discover_session_files(tmp_path) == []

    def test_nonexistent_dir(self, tmp_path: Path) -> None:
        assert discover_session_files(tmp_path / "nope") == []

    def test_finds_json_files(self, tmp_path: Path) -> None:
        (tmp_path / "sess1.json").write_text("{}")
        (tmp_path / "sess2.json").write_text("{}")
        (tmp_path / "readme.txt").write_text("ignore")
        files = discover_session_files(tmp_path)
        assert len(files) == 2
        assert all(f.suffix == ".json" for f in files)

    def test_finds_nested_files(self, tmp_path: Path) -> None:
        subdir = tmp_path / "project"
        subdir.mkdir()
        (subdir / "sess.json").write_text("{}")
        files = discover_session_files(tmp_path)
        assert len(files) == 1


class TestReadSessionFile:
    def test_valid_json(self, tmp_path: Path) -> None:
        p = tmp_path / "s.json"
        p.write_text('{"id": "abc", "messages": []}')
        data = read_session_file(p)
        assert data is not None
        assert data["id"] == "abc"

    def test_invalid_json(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.json"
        p.write_text("not json")
        assert read_session_file(p) is None

    def test_array_json(self, tmp_path: Path) -> None:
        p = tmp_path / "arr.json"
        p.write_text("[1, 2, 3]")
        assert read_session_file(p) is None


class TestExtractContent:
    def test_string_content(self) -> None:
        assert _extract_content({"content": "hello"}) == "hello"

    def test_list_content(self) -> None:
        msg = {"content": [{"text": "part1"}, {"text": "part2"}]}
        result = _extract_content(msg)
        assert "part1" in result
        assert "part2" in result

    def test_empty_content(self) -> None:
        assert _extract_content({}) == ""

    def test_none_content(self) -> None:
        assert _extract_content({"content": None}) == ""


class TestMapMessage:
    def test_user_message(self) -> None:
        events = _map_message({"role": "user", "content": "fix the bug"})
        assert len(events) == 1
        assert events[0].type == "prompt"
        assert events[0].content == "fix the bug"

    def test_assistant_message(self) -> None:
        events = _map_message({"role": "assistant", "content": "done"})
        assert len(events) == 1
        assert events[0].type == "response"

    def test_tool_calls(self) -> None:
        msg = {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"function": {"name": "read_file", "arguments": '{"path": "foo.py"}'}},
            ],
        }
        events = _map_message(msg)
        assert any(e.type == "tool_call" for e in events)
        tc = [e for e in events if e.type == "tool_call"][0]
        assert "read_file" in tc.content

    def test_file_changes_in_parts(self) -> None:
        msg = {
            "role": "assistant",
            "content": "updated",
            "parts": [
                {"type": "file_change", "path": "src/main.py", "change": "modified"},
            ],
        }
        events = _map_message(msg)
        fc = [e for e in events if e.type == "file_change"]
        assert len(fc) == 1
        assert "src/main.py" in fc[0].content

    def test_empty_message(self) -> None:
        events = _map_message({"role": "user", "content": ""})
        assert events == []


class TestImportSession:
    def test_basic_import(self, engine_no_sensitive: MemoryEngine) -> None:
        data = {
            "id": "test-sess-1",
            "title": "Test Session",
            "messages": [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi there"},
            ],
        }
        result = import_session(engine_no_sensitive, data)
        assert result["event_count"] == 2
        assert result["source_id"] == "test-sess-1"
        assert result["errors"] == []

    def test_empty_messages(self, engine_no_sensitive: MemoryEngine) -> None:
        data = {"id": "empty", "title": "Empty", "messages": []}
        result = import_session(engine_no_sensitive, data)
        assert result["event_count"] == 0


class TestImportSessions:
    def _write_session(self, d: Path, sid: str, msgs: list) -> None:
        data = {"id": sid, "title": f"Session {sid}", "messages": msgs}
        (d / f"{sid}.json").write_text(json.dumps(data))

    def test_dry_run(self, tmp_path: Path, engine_no_sensitive: MemoryEngine) -> None:
        self._write_session(tmp_path, "s1", [{"role": "user", "content": "hi"}])
        results = import_sessions(
            engine_no_sensitive, tmp_path, dry_run=True, import_all=True
        )
        assert len(results) == 1
        assert results[0]["dry_run"] is True

    def test_import_all(self, tmp_path: Path, engine_no_sensitive: MemoryEngine) -> None:
        self._write_session(tmp_path, "s1", [{"role": "user", "content": "hi"}])
        self._write_session(tmp_path, "s2", [{"role": "user", "content": "bye"}])
        results = import_sessions(
            engine_no_sensitive, tmp_path, import_all=True
        )
        assert len(results) == 2
        assert all("session_id" in r for r in results)

    def test_import_specific_session(
        self, tmp_path: Path, engine_no_sensitive: MemoryEngine
    ) -> None:
        self._write_session(tmp_path, "s1", [{"role": "user", "content": "hi"}])
        self._write_session(tmp_path, "s2", [{"role": "user", "content": "bye"}])
        results = import_sessions(
            engine_no_sensitive, tmp_path, session_id="s2", import_all=True
        )
        assert len(results) == 1
        assert results[0]["source_id"] == "s2"

    def test_list_mode(self, tmp_path: Path, engine_no_sensitive: MemoryEngine) -> None:
        self._write_session(tmp_path, "s1", [{"role": "user", "content": "hi"}])
        results = import_sessions(engine_no_sensitive, tmp_path)
        assert len(results) == 1
        assert results[0].get("dry_run") is True

    def test_no_files(self, tmp_path: Path, engine_no_sensitive: MemoryEngine) -> None:
        results = import_sessions(engine_no_sensitive, tmp_path)
        assert results == []
