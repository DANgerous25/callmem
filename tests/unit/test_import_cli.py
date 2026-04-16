"""Tests for the import CLI command."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from click.testing import CliRunner

from llm_mem.cli import main

if TYPE_CHECKING:
    from pathlib import Path


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
        assert "--session-dir" in result.output
        assert "--all" in result.output
        assert "--dry-run" in result.output


class TestImportCommand:
    def _setup_project(self, runner: CliRunner, project: Path) -> None:
        runner.invoke(main, ["init", "--project", str(project)])

    def _write_session(self, d: Path, sid: str, msgs: list) -> None:
        d.mkdir(parents=True, exist_ok=True)
        data = {"id": sid, "title": f"Session {sid}", "messages": msgs}
        (d / f"{sid}.json").write_text(json.dumps(data))

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
        session_dir = tmp_path / "empty_sessions"
        session_dir.mkdir()
        result = runner.invoke(
            main,
            [
                "import", "--source", "opencode",
                "--project", str(tmp_path),
                "--session-dir", str(session_dir),
            ],
        )
        assert "No sessions found" in result.output

    def test_list_mode(self, tmp_path: Path) -> None:
        runner = CliRunner()
        self._setup_project(runner, tmp_path)
        session_dir = tmp_path / "sessions"
        self._write_session(
            session_dir, "s1", [{"role": "user", "content": "hello"}]
        )
        result = runner.invoke(
            main,
            [
                "import", "--source", "opencode",
                "--project", str(tmp_path),
                "--session-dir", str(session_dir),
            ],
        )
        assert result.exit_code == 0
        assert "s1" in result.output
        assert "Use --all to import all" in result.output

    def test_dry_run(self, tmp_path: Path) -> None:
        runner = CliRunner()
        self._setup_project(runner, tmp_path)
        session_dir = tmp_path / "sessions"
        self._write_session(
            session_dir, "s1", [{"role": "user", "content": "hello"}]
        )
        result = runner.invoke(
            main,
            [
                "import", "--source", "opencode",
                "--project", str(tmp_path),
                "--session-dir", str(session_dir),
                "--dry-run",
            ],
        )
        assert result.exit_code == 0
        assert "dry-run" in result.output

    def test_import_all(self, tmp_path: Path) -> None:
        runner = CliRunner()
        self._setup_project(runner, tmp_path)
        session_dir = tmp_path / "sessions"
        self._write_session(
            session_dir, "s1",
            [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"}],
        )
        result = runner.invoke(
            main,
            [
                "import", "--source", "opencode",
                "--project", str(tmp_path),
                "--session-dir", str(session_dir),
                "--all",
            ],
        )
        assert result.exit_code == 0
        assert "Imported" in result.output
        assert "events" in result.output

    def test_import_specific_session(self, tmp_path: Path) -> None:
        runner = CliRunner()
        self._setup_project(runner, tmp_path)
        session_dir = tmp_path / "sessions"
        self._write_session(
            session_dir, "target",
            [{"role": "user", "content": "fix bug"}],
        )
        self._write_session(
            session_dir, "other",
            [{"role": "user", "content": "ignore me"}],
        )
        result = runner.invoke(
            main,
            [
                "import", "--source", "opencode",
                "--project", str(tmp_path),
                "--session-dir", str(session_dir),
                "--session-id", "target",
            ],
        )
        assert result.exit_code == 0
        assert "target" in result.output
