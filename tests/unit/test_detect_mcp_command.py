"""Tests for ``detect_mcp_command`` PATH-independence.

OpenCode and Claude Code spawn MCP subprocesses with cwd set to the
project, and may auto-activate a project-local ``.venv/`` ahead of PATH.
A bare ``python3`` invocation is unreliable in that environment, so the
detector must always emit absolute interpreter paths.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

from callmem.core.integrations import detect_mcp_command

if TYPE_CHECKING:
    import pytest


class TestDetectMcpCommand:
    def test_uses_sys_executable_when_no_venv(self, tmp_path: Path) -> None:
        """No project venv → use the wizard's sys.executable (absolute)."""
        cmd = detect_mcp_command(tmp_path)

        assert cmd[0] == sys.executable
        assert Path(cmd[0]).is_absolute()
        assert cmd[1:4] == ["-m", "callmem.mcp.server", "--project"]
        assert cmd[4] == str(tmp_path.resolve())

    def test_project_path_is_absolute(self, tmp_path: Path) -> None:
        """``--project`` arg must be absolute, not '.', so the spawned
        subprocess works regardless of cwd."""
        cmd = detect_mcp_command(tmp_path)

        project_idx = cmd.index("--project")
        project_arg = cmd[project_idx + 1]
        assert Path(project_arg).is_absolute()
        assert project_arg == str(tmp_path.resolve())

    def test_prefers_project_venv_when_callmem_importable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If ``<project>/.venv/bin/python`` exists and can import callmem,
        prefer it over sys.executable."""
        venv_bin = tmp_path / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        venv_python = venv_bin / "python"
        venv_python.touch()

        from callmem.core import integrations
        monkeypatch.setattr(
            integrations, "_can_import_callmem",
            lambda p: str(p) == str(venv_python),
        )

        cmd = detect_mcp_command(tmp_path)
        assert cmd[0] == str(venv_python)

    def test_skips_project_venv_without_callmem(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If the project venv exists but doesn't have callmem, fall back
        to sys.executable rather than producing a broken command."""
        venv_bin = tmp_path / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        (venv_bin / "python").touch()

        from callmem.core import integrations
        monkeypatch.setattr(integrations, "_can_import_callmem", lambda p: False)

        cmd = detect_mcp_command(tmp_path)
        assert cmd[0] == sys.executable

    def test_no_bare_python3(self, tmp_path: Path) -> None:
        """Regression: previously emitted ``python3`` by name, which the
        agent's subprocess would resolve via PATH (often to a venv that
        doesn't have callmem)."""
        cmd = detect_mcp_command(tmp_path)
        assert cmd[0] != "python3"
        assert cmd[0] != "python"
