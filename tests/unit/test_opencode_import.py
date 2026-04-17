"""Tests for OpenCode session history importer (SQLite-based)."""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING

import pytest

from llm_mem.adapters.opencode_import import (
    _map_message,
    _parse_json,
    _progress_path,
    discover_sessions,
    import_session,
    import_sessions,
    read_import_progress,
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


def _create_opencode_db(db_path: Path) -> sqlite3.Connection:
    """Create a minimal OpenCode-style SQLite database for testing."""
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE project (
            id TEXT PRIMARY KEY,
            worktree TEXT NOT NULL,
            name TEXT,
            time_created INTEGER NOT NULL,
            time_updated INTEGER NOT NULL,
            sandboxes TEXT NOT NULL DEFAULT '[]'
        )
    """)
    conn.execute("""
        CREATE TABLE session (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES project(id),
            slug TEXT NOT NULL DEFAULT '',
            directory TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL,
            version TEXT NOT NULL DEFAULT '1',
            time_created INTEGER NOT NULL,
            time_updated INTEGER NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE message (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL REFERENCES session(id),
            time_created INTEGER NOT NULL,
            time_updated INTEGER NOT NULL,
            data TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE part (
            id TEXT PRIMARY KEY,
            message_id TEXT NOT NULL REFERENCES message(id),
            session_id TEXT NOT NULL,
            time_created INTEGER NOT NULL,
            time_updated INTEGER NOT NULL,
            data TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


def _add_project(conn: sqlite3.Connection, pid: str, worktree: str, name: str) -> None:
    conn.execute(
        "INSERT INTO project (id, worktree, name, time_created, time_updated, sandboxes)"
        " VALUES (?, ?, ?, 1000, 1000, '[]')",
        (pid, worktree, name),
    )
    conn.commit()


def _add_session(
    conn: sqlite3.Connection, sid: str, project_id: str, title: str, ts: int = 2000,
) -> None:
    conn.execute(
        "INSERT INTO session (id, project_id, title, time_created, time_updated)"
        " VALUES (?, ?, ?, ?, ?)",
        (sid, project_id, title, ts, ts),
    )
    conn.commit()


def _add_message(
    conn: sqlite3.Connection,
    mid: str,
    session_id: str,
    role: str,
    ts: int = 3000,
) -> None:
    data = json.dumps({"role": role})
    conn.execute(
        "INSERT INTO message (id, session_id, time_created, time_updated, data)"
        " VALUES (?, ?, ?, ?, ?)",
        (mid, session_id, ts, ts, data),
    )
    conn.commit()


def _add_part(
    conn: sqlite3.Connection,
    part_id: str,
    message_id: str,
    session_id: str,
    part_data: dict,
    ts: int = 3000,
) -> None:
    conn.execute(
        "INSERT INTO part (id, message_id, session_id, time_created, time_updated, data)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (part_id, message_id, session_id, ts, ts, json.dumps(part_data)),
    )
    conn.commit()


class TestParseJson:
    def test_valid(self) -> None:
        assert _parse_json('{"a": 1}') == {"a": 1}

    def test_invalid(self) -> None:
        assert _parse_json("not json") == {}

    def test_none(self) -> None:
        assert _parse_json(None) == {}

    def test_array(self) -> None:
        assert _parse_json("[1,2]") == {}


class TestDiscoverSessions:
    def test_no_db(self, tmp_path: Path) -> None:
        result = discover_sessions(db_path=tmp_path / "nonexistent.db")
        assert result == []

    def test_empty_db(self, tmp_path: Path) -> None:
        db_path = tmp_path / "opencode.db"
        _create_opencode_db(db_path)
        result = discover_sessions(db_path=db_path)
        assert result == []

    def test_finds_sessions(self, tmp_path: Path) -> None:
        db_path = tmp_path / "opencode.db"
        conn = _create_opencode_db(db_path)
        _add_project(conn, "p1", "/home/user/myproject", "myproject")
        _add_session(conn, "s1", "p1", "First Session")
        _add_session(conn, "s2", "p1", "Second Session")
        conn.close()

        result = discover_sessions(db_path=db_path)
        assert len(result) == 2
        assert result[0]["id"] == "s1"
        assert result[0]["project_name"] == "myproject"
        assert result[0]["project_worktree"] == "/home/user/myproject"

    def test_filter_by_project_path(self, tmp_path: Path) -> None:
        db_path = tmp_path / "opencode.db"
        conn = _create_opencode_db(db_path)
        _add_project(conn, "p1", "/home/user/projectA", "projA")
        _add_project(conn, "p2", "/home/user/projectB", "projB")
        _add_session(conn, "s1", "p1", "Session A")
        _add_session(conn, "s2", "p2", "Session B")
        conn.close()

        result = discover_sessions(
            db_path=db_path, project_path="/home/user/projectA"
        )
        assert len(result) == 1
        assert result[0]["project_name"] == "projA"

    def test_message_count(self, tmp_path: Path) -> None:
        db_path = tmp_path / "opencode.db"
        conn = _create_opencode_db(db_path)
        _add_project(conn, "p1", "/tmp/proj", "proj")
        _add_session(conn, "s1", "p1", "Session")
        _add_message(conn, "m1", "s1", "user", ts=3000)
        _add_message(conn, "m2", "s1", "assistant", ts=4000)
        conn.close()

        result = discover_sessions(db_path=db_path)
        assert result[0]["message_count"] == 2


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
                {"name": "read_file", "args": '{"path": "foo.py"}'},
            ],
        }
        events = _map_message(msg)
        assert any(e.type == "tool_call" for e in events)
        tc = [e for e in events if e.type == "tool_call"][0]
        assert "read_file" in tc.content

    def test_file_changes(self) -> None:
        msg = {
            "role": "assistant",
            "content": "updated",
            "file_changes": [
                {"path": "src/main.py", "change": "modified"},
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
        session_data = {"id": "test-sess-1", "title": "Test Session"}
        messages = [
            {"role": "user", "content": "hello", "tool_calls": [], "file_changes": []},
            {"role": "assistant", "content": "hi there", "tool_calls": [], "file_changes": []},
        ]
        result = import_session(engine_no_sensitive, session_data, messages)
        assert result["event_count"] == 2
        assert result["source_id"] == "test-sess-1"
        assert result["errors"] == []

    def test_empty_messages(self, engine_no_sensitive: MemoryEngine) -> None:
        session_data = {"id": "empty", "title": "Empty"}
        result = import_session(engine_no_sensitive, session_data, [])
        assert result["event_count"] == 0


class TestImportSessions:
    def test_no_db(self, tmp_path: Path, engine_no_sensitive: MemoryEngine) -> None:
        results = import_sessions(
            engine_no_sensitive,
            db_path=tmp_path / "nonexistent.db",
        )
        assert results == []

    def test_dry_run(self, tmp_path: Path, engine_no_sensitive: MemoryEngine) -> None:
        db_path = tmp_path / "opencode.db"
        conn = _create_opencode_db(db_path)
        _add_project(conn, "p1", "/tmp/proj", "proj")
        _add_session(conn, "s1", "p1", "Session 1")
        _add_message(conn, "m1", "s1", "user")
        _add_part(conn, "pt1", "m1", "s1", {"type": "text", "text": "hi"})
        conn.close()

        results = import_sessions(
            engine_no_sensitive, db_path=db_path, dry_run=True, import_all=True
        )
        assert len(results) == 1
        assert results[0]["dry_run"] is True

    def test_import_all(self, tmp_path: Path, engine_no_sensitive: MemoryEngine) -> None:
        db_path = tmp_path / "opencode.db"
        conn = _create_opencode_db(db_path)
        _add_project(conn, "p1", "/tmp/proj", "proj")
        _add_session(conn, "s1", "p1", "Session 1", ts=1000)
        _add_message(conn, "m1", "s1", "user", ts=2000)
        _add_part(conn, "pt1", "m1", "s1", {"type": "text", "text": "hi"})
        _add_session(conn, "s2", "p1", "Session 2", ts=3000)
        _add_message(conn, "m2", "s2", "user", ts=4000)
        _add_part(conn, "pt2", "m2", "s2", {"type": "text", "text": "bye"})
        conn.close()

        results = import_sessions(
            engine_no_sensitive, db_path=db_path, import_all=True
        )
        assert len(results) == 2
        assert all("session_id" in r for r in results)

    def test_import_specific_session(
        self, tmp_path: Path, engine_no_sensitive: MemoryEngine
    ) -> None:
        db_path = tmp_path / "opencode.db"
        conn = _create_opencode_db(db_path)
        _add_project(conn, "p1", "/tmp/proj", "proj")
        _add_session(conn, "s1", "p1", "Session 1")
        _add_session(conn, "s2", "p1", "Session 2")
        conn.close()

        results = import_sessions(
            engine_no_sensitive, db_path=db_path, session_id="s2", import_all=True
        )
        assert len(results) == 1
        assert results[0]["source_id"] == "s2"

    def test_list_mode(self, tmp_path: Path, engine_no_sensitive: MemoryEngine) -> None:
        db_path = tmp_path / "opencode.db"
        conn = _create_opencode_db(db_path)
        _add_project(conn, "p1", "/tmp/proj", "proj")
        _add_session(conn, "s1", "p1", "Session 1")
        conn.close()

        results = import_sessions(engine_no_sensitive, db_path=db_path)
        assert len(results) == 1
        assert results[0].get("dry_run") is True

    def test_filter_by_project_path(
        self, tmp_path: Path, engine_no_sensitive: MemoryEngine
    ) -> None:
        db_path = tmp_path / "opencode.db"
        conn = _create_opencode_db(db_path)
        _add_project(conn, "p1", "/home/user/projA", "projA")
        _add_project(conn, "p2", "/home/user/projB", "projB")
        _add_session(conn, "s1", "p1", "Session A")
        _add_session(conn, "s2", "p2", "Session B")
        conn.close()

        results = import_sessions(
            engine_no_sensitive,
            db_path=db_path,
            project_path="/home/user/projA",
            import_all=True,
        )
        assert len(results) == 1
        assert results[0]["source_id"] == "s1"

    def test_tool_calls_imported(
        self, tmp_path: Path, engine_no_sensitive: MemoryEngine
    ) -> None:
        db_path = tmp_path / "opencode.db"
        conn = _create_opencode_db(db_path)
        _add_project(conn, "p1", "/tmp/proj", "proj")
        _add_session(conn, "s1", "p1", "Tool Session")
        _add_message(conn, "m1", "s1", "assistant", ts=3000)
        _add_part(conn, "pt1", "m1", "s1", {
            "type": "tool-invocation",
            "toolName": "read_file",
            "args": {"path": "foo.py"},
        })
        conn.close()

        results = import_sessions(
            engine_no_sensitive, db_path=db_path, import_all=True
        )
        assert len(results) == 1
        assert results[0]["event_count"] == 1


class TestProgressCallback:
    def test_progress_callback_receives_updates(
        self, tmp_path: Path, engine_no_sensitive: MemoryEngine
    ) -> None:
        db_path = tmp_path / "opencode.db"
        conn = _create_opencode_db(db_path)
        _add_project(conn, "p1", "/tmp/proj", "proj")
        _add_session(conn, "s1", "p1", "Session 1", ts=1000)
        _add_message(conn, "m1", "s1", "user", ts=2000)
        _add_part(conn, "pt1", "m1", "s1", {"type": "text", "text": "hi"})
        _add_session(conn, "s2", "p1", "Session 2", ts=3000)
        _add_message(conn, "m2", "s2", "user", ts=4000)
        _add_part(conn, "pt2", "m2", "s2", {"type": "text", "text": "bye"})
        conn.close()

        updates: list[dict] = []
        results = import_sessions(
            engine_no_sensitive,
            db_path=db_path,
            import_all=True,
            progress_callback=updates.append,
        )

        assert len(results) == 2
        phases = [u.get("phase") for u in updates]
        assert "discovery" in phases
        assert "importing" in phases
        assert any(u.get("session_index") == 1 for u in updates)
        assert any(u.get("session_index") == 2 for u in updates)

    def test_progress_callback_with_title(
        self, tmp_path: Path, engine_no_sensitive: MemoryEngine
    ) -> None:
        db_path = tmp_path / "opencode.db"
        conn = _create_opencode_db(db_path)
        _add_project(conn, "p1", "/tmp/proj", "proj")
        _add_session(conn, "s1", "p1", "My Special Session", ts=1000)
        _add_message(conn, "m1", "s1", "user", ts=2000)
        _add_part(conn, "pt1", "m1", "s1", {"type": "text", "text": "test"})
        conn.close()

        updates: list[dict] = []
        import_sessions(
            engine_no_sensitive,
            db_path=db_path,
            import_all=True,
            progress_callback=updates.append,
        )

        importing = [u for u in updates if u.get("phase") == "importing"]
        assert len(importing) == 1
        assert importing[0]["session_title"] == "My Special Session"

    def test_no_progress_callback(
        self, tmp_path: Path, engine_no_sensitive: MemoryEngine
    ) -> None:
        db_path = tmp_path / "opencode.db"
        conn = _create_opencode_db(db_path)
        _add_project(conn, "p1", "/tmp/proj", "proj")
        _add_session(conn, "s1", "p1", "Session 1", ts=1000)
        _add_message(conn, "m1", "s1", "user", ts=2000)
        _add_part(conn, "pt1", "m1", "s1", {"type": "text", "text": "hi"})
        conn.close()

        results = import_sessions(
            engine_no_sensitive, db_path=db_path, import_all=True
        )
        assert len(results) == 1


class TestProgressFile:
    def test_progress_file_written(
        self, tmp_path: Path, engine_no_sensitive: MemoryEngine
    ) -> None:
        db_path = tmp_path / "opencode.db"
        conn = _create_opencode_db(db_path)
        _add_project(conn, "p1", "/tmp/proj", "proj")
        _add_session(conn, "s1", "p1", "Session 1", ts=1000)
        _add_message(conn, "m1", "s1", "user", ts=2000)
        _add_part(conn, "pt1", "m1", "s1", {"type": "text", "text": "hi"})
        conn.close()

        import_sessions(
            engine_no_sensitive,
            db_path=db_path,
            import_all=True,
            project=tmp_path,
        )

        progress = read_import_progress(tmp_path)
        assert progress["status"] == "completed"
        assert progress["imported_sessions"] == 1
        assert progress["imported_events"] == 1
        assert "started_at" in progress
        assert "completed_at" in progress

    def test_progress_file_not_written_without_project(
        self, tmp_path: Path, engine_no_sensitive: MemoryEngine
    ) -> None:
        db_path = tmp_path / "opencode.db"
        conn = _create_opencode_db(db_path)
        _add_project(conn, "p1", "/tmp/proj", "proj")
        _add_session(conn, "s1", "p1", "Session 1", ts=1000)
        _add_message(conn, "m1", "s1", "user", ts=2000)
        _add_part(conn, "pt1", "m1", "s1", {"type": "text", "text": "hi"})
        conn.close()

        import_sessions(
            engine_no_sensitive,
            db_path=db_path,
            import_all=True,
        )

        progress_file = _progress_path(tmp_path)
        assert not progress_file.exists()

    def test_read_import_progress_no_file(self, tmp_path: Path) -> None:
        result = read_import_progress(tmp_path)
        assert result == {}

    def test_read_import_progress_corrupt(self, tmp_path: Path) -> None:
        progress_file = _progress_path(tmp_path)
        progress_file.parent.mkdir(parents=True, exist_ok=True)
        progress_file.write_text("not json")
        result = read_import_progress(tmp_path)
        assert result == {}

    def test_progress_updated_incrementally(
        self, tmp_path: Path, engine_no_sensitive: MemoryEngine
    ) -> None:
        db_path = tmp_path / "opencode.db"
        conn = _create_opencode_db(db_path)
        _add_project(conn, "p1", "/tmp/proj", "proj")
        _add_session(conn, "s1", "p1", "Session 1", ts=1000)
        _add_message(conn, "m1", "s1", "user", ts=2000)
        _add_part(conn, "pt1", "m1", "s1", {"type": "text", "text": "hi"})
        _add_session(conn, "s2", "p1", "Session 2", ts=3000)
        _add_message(conn, "m2", "s2", "user", ts=4000)
        _add_part(conn, "pt2", "m2", "s2", {"type": "text", "text": "bye"})
        conn.close()

        snapshots: list[dict] = []

        def _capture(progress: dict) -> None:
            if progress.get("phase") == "importing":
                from time import sleep
                sleep(0.05)
                pf = _progress_path(tmp_path)
                if pf.exists():
                    snapshots.append(json.loads(pf.read_text()))

        import_sessions(
            engine_no_sensitive,
            db_path=db_path,
            import_all=True,
            progress_callback=_capture,
            project=tmp_path,
        )

        assert len(snapshots) >= 1
        for s in snapshots:
            assert s["status"] == "running"
        if len(snapshots) >= 2:
            assert snapshots[1]["imported_sessions"] >= snapshots[0]["imported_sessions"]


class TestLockfile:
    def test_lockfile_prevents_concurrent(
        self, tmp_path: Path, engine_no_sensitive: MemoryEngine
    ) -> None:
        from llm_mem.adapters.opencode_import import _acquire_lock, _release_lock

        db_path = tmp_path / "opencode.db"
        conn = _create_opencode_db(db_path)
        _add_project(conn, "p1", "/tmp/proj", "proj")
        _add_session(conn, "s1", "p1", "Session 1", ts=1000)
        conn.close()

        lock_fd = _acquire_lock(tmp_path)
        assert lock_fd is not None

        with pytest.raises(RuntimeError, match="already in progress"):
            import_sessions(
                engine_no_sensitive,
                db_path=db_path,
                import_all=True,
                project=tmp_path,
            )

        _release_lock(lock_fd)

    def test_lockfile_released_after_import(
        self, tmp_path: Path, engine_no_sensitive: MemoryEngine
    ) -> None:
        from llm_mem.adapters.opencode_import import _acquire_lock

        db_path = tmp_path / "opencode.db"
        conn = _create_opencode_db(db_path)
        _add_project(conn, "p1", "/tmp/proj", "proj")
        _add_session(conn, "s1", "p1", "Session 1", ts=1000)
        _add_message(conn, "m1", "s1", "user", ts=2000)
        _add_part(conn, "pt1", "m1", "s1", {"type": "text", "text": "hi"})
        conn.close()

        import_sessions(
            engine_no_sensitive,
            db_path=db_path,
            import_all=True,
            project=tmp_path,
        )

        lock_fd = _acquire_lock(tmp_path)
        from llm_mem.adapters.opencode_import import _release_lock

        _release_lock(lock_fd)
