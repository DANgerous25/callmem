"""Regression tests for setup wizard session-import scoping.

Both the foreground (`_offer_session_import`) and background
(`_run_setup_background_import`) paths must scope imports to the
current project's worktree. A prior bug let them ingest sessions
from every worktree, polluting sibling project DBs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

from callmem import setup_wizard as setup


class TestBackgroundImportCommand:
    def test_passes_project_path_flag(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}

        class _FakeProc:
            pid = 12345

        def _fake_popen(cmd: list[str], **kwargs: Any) -> _FakeProc:
            captured["cmd"] = cmd
            return _FakeProc()

        import subprocess

        monkeypatch.setattr(subprocess, "Popen", _fake_popen)

        project = tmp_path / "my-project"
        project.mkdir()
        oc_db = tmp_path / "opencode.db"
        oc_db.write_bytes(b"")

        setup._run_setup_background_import(project, oc_db)

        cmd = captured["cmd"]
        assert "--project-path" in cmd, cmd
        pp_idx = cmd.index("--project-path")
        assert cmd[pp_idx + 1] == str(project)
        assert "--project" in cmd and "--all" in cmd


class TestForegroundImportPassesProjectPath:
    def test_import_sessions_receives_project_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`_offer_session_import` must forward project_path so the
        second internal `discover_sessions()` call inside `import_sessions`
        also filters by worktree."""
        calls: list[dict[str, Any]] = []

        def _fake_discover(
            db_path: Path | None = None,
            project_path: str | None = None,
        ) -> list[dict[str, Any]]:
            return [
                {
                    "id": "s1",
                    "title": "t",
                    "project_id": "p",
                    "project_name": "n",
                    "project_worktree": project_path or "/other",
                    "message_count": 1,
                    "time_created": 0,
                }
            ]

        def _fake_import_sessions(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
            calls.append(kwargs)
            return []

        monkeypatch.setattr(
            "callmem.adapters.opencode_import.discover_sessions",
            _fake_discover,
        )
        monkeypatch.setattr(
            "callmem.adapters.opencode_import.import_sessions",
            _fake_import_sessions,
        )
        monkeypatch.setattr(
            "callmem.adapters.opencode_import.DEFAULT_DB_PATH",
            tmp_path / "opencode.db",
        )
        (tmp_path / "opencode.db").write_bytes(b"")

        monkeypatch.setattr(setup, "ask_bool", lambda *a, **k: True)
        monkeypatch.setattr(setup, "ask", lambda *a, **k: "1")

        project = tmp_path / "proj"
        project.mkdir()
        db_path = project / ".callmem" / "memory.db"
        db_path.parent.mkdir(parents=True)
        db_path.write_bytes(b"")

        monkeypatch.setattr(
            "callmem.core.config.load_config", lambda _p: _StubConfig()
        )
        monkeypatch.setattr(
            "callmem.core.database.Database",
            lambda _p: _StubDB(),
        )
        monkeypatch.setattr(
            "callmem.core.engine.MemoryEngine",
            lambda _db, _cfg: object(),
        )

        setup._offer_session_import(project, db_path)

        assert calls, "import_sessions was not called"
        assert calls[0].get("project_path") == str(project), calls[0]


class _StubConfig:
    pass


class _StubDB:
    def initialize(self) -> None:
        pass
