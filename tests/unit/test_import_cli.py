"""Tests for the import CLI command."""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING

from click.testing import CliRunner

from llm_mem.cli import main

if TYPE_CHECKING:
    from pathlib import Path


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


def _populate_session(
    conn: sqlite3.Connection,
    project_id: str,
    worktree: str,
    project_name: str,
    session_id: str,
    title: str,
    messages: list[tuple[str, str]],
) -> None:
    """Add a project, session, and messages+parts to the test DB."""
    conn.execute(
        "INSERT OR IGNORE INTO project"
        " (id, worktree, name, time_created, time_updated, sandboxes)"
        " VALUES (?, ?, ?, 1000, 1000, '[]')",
        (project_id, worktree, project_name),
    )
    conn.execute(
        "INSERT INTO session"
        " (id, project_id, title, time_created, time_updated)"
        " VALUES (?, ?, ?, 2000, 2000)",
        (session_id, project_id, title),
    )
    for i, (role, content) in enumerate(messages):
        mid = f"{session_id}-m{i}"
        conn.execute(
            "INSERT INTO message"
            " (id, session_id, time_created, time_updated, data)"
            " VALUES (?, ?, ?, ?, ?)",
            (mid, session_id, 3000 + i, 3000 + i, json.dumps({"role": role})),
        )
        conn.execute(
            "INSERT INTO part"
            " (id, message_id, session_id, time_created, time_updated, data)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                f"{mid}-p0", mid, session_id, 3000 + i, 3000 + i,
                json.dumps({"type": "text", "text": content}),
            ),
        )
    conn.commit()


class TestImportHelp:
    def test_help_shows_import(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["--help"])
        assert result.exit_code == 0
        assert "import" in result.output

    def test_import_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["import", "--help"])
        assert result.exit_code == 0
        assert "--source" in result.output
        assert "--session-id" in result.output
        assert "--opencode-db" in result.output
        assert "--all" in result.output
        assert "--dry-run" in result.output


class TestImportCommand:
    def _setup_project(self, runner: CliRunner, project: Path) -> None:
        runner.invoke(main, ["init", "--project", str(project)])

    def test_no_database(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["import", "--source", "opencode", "--project", str(tmp_path)],
        )
        assert "No llm-mem database" in result.output

    def test_no_sessions_found(self, tmp_path: Path) -> None:
        runner = CliRunner()
        self._setup_project(runner, tmp_path)
        oc_db = tmp_path / "opencode.db"
        _create_opencode_db(oc_db).close()
        result = runner.invoke(
            main,
            [
                "import", "--source", "opencode",
                "--project", str(tmp_path),
                "--opencode-db", str(oc_db),
            ],
        )
        assert "No sessions found" in result.output

    def test_list_mode(self, tmp_path: Path) -> None:
        runner = CliRunner()
        self._setup_project(runner, tmp_path)
        oc_db = tmp_path / "opencode.db"
        conn = _create_opencode_db(oc_db)
        _populate_session(
            conn, "p1", "/tmp/proj", "proj",
            "s1", "Session 1", [("user", "hello")],
        )
        conn.close()
        result = runner.invoke(
            main,
            [
                "import", "--source", "opencode",
                "--project", str(tmp_path),
                "--opencode-db", str(oc_db),
            ],
        )
        assert result.exit_code == 0
        assert "s1" in result.output
        assert "Use --all to import all" in result.output

    def test_dry_run(self, tmp_path: Path) -> None:
        runner = CliRunner()
        self._setup_project(runner, tmp_path)
        oc_db = tmp_path / "opencode.db"
        conn = _create_opencode_db(oc_db)
        _populate_session(
            conn, "p1", "/tmp/proj", "proj",
            "s1", "Session 1", [("user", "hello")],
        )
        conn.close()
        result = runner.invoke(
            main,
            [
                "import", "--source", "opencode",
                "--project", str(tmp_path),
                "--opencode-db", str(oc_db),
                "--dry-run",
            ],
        )
        assert result.exit_code == 0
        assert "dry-run" in result.output

    def test_import_all(self, tmp_path: Path) -> None:
        runner = CliRunner()
        self._setup_project(runner, tmp_path)
        oc_db = tmp_path / "opencode.db"
        conn = _create_opencode_db(oc_db)
        _populate_session(
            conn, "p1", "/tmp/proj", "proj",
            "s1", "Session 1",
            [("user", "hello"), ("assistant", "hi")],
        )
        conn.close()
        result = runner.invoke(
            main,
            [
                "import", "--source", "opencode",
                "--project", str(tmp_path),
                "--opencode-db", str(oc_db),
                "--all",
            ],
        )
        assert result.exit_code == 0
        assert "Imported" in result.output
        assert "events" in result.output

    def test_import_specific_session(self, tmp_path: Path) -> None:
        runner = CliRunner()
        self._setup_project(runner, tmp_path)
        oc_db = tmp_path / "opencode.db"
        conn = _create_opencode_db(oc_db)
        _populate_session(
            conn, "p1", "/tmp/proj", "proj",
            "target", "Target Session", [("user", "fix bug")],
        )
        _populate_session(
            conn, "p1", "/tmp/proj", "proj",
            "other", "Other Session", [("user", "ignore me")],
        )
        conn.close()
        result = runner.invoke(
            main,
            [
                "import", "--source", "opencode",
                "--project", str(tmp_path),
                "--opencode-db", str(oc_db),
                "--session-id", "target",
            ],
        )
        assert result.exit_code == 0
        assert "target" in result.output
