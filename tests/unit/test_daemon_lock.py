"""Tests for daemon pid files, env-file key fallback, and MCP worker gating."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

from callmem.core.config import resolve_secret
from callmem.core.daemon_lock import (
    daemon_running,
    pid_file_path,
    remove_pid_file,
    write_pid_file,
)
from callmem.core.ollama import OllamaClient
from callmem.core.openai_compat import OpenAICompatClient
from callmem.mcp.server import _worker_skip_reason


class TestDaemonPidFile:
    def test_write_records_own_pid(self, tmp_path: Path) -> None:
        write_pid_file(tmp_path)
        assert pid_file_path(tmp_path).read_text().strip() == str(os.getpid())

    def test_not_running_without_file(self, tmp_path: Path) -> None:
        assert daemon_running(tmp_path) is False

    def test_dead_pid_is_not_running(self, tmp_path: Path) -> None:
        path = pid_file_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("999999999\n")
        assert daemon_running(tmp_path) is False

    def test_reused_pid_of_non_callmem_process_is_not_running(
        self, tmp_path: Path
    ) -> None:
        path = pid_file_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("1\n")  # init/systemd: alive, but not callmem
        assert daemon_running(tmp_path) is False

    def test_malformed_file_is_not_running(self, tmp_path: Path) -> None:
        path = pid_file_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("garbage")
        assert daemon_running(tmp_path) is False

    def test_remove_only_own_pid(self, tmp_path: Path) -> None:
        path = pid_file_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("1\n")
        remove_pid_file(tmp_path)
        assert path.exists()
        write_pid_file(tmp_path)
        remove_pid_file(tmp_path)
        assert not path.exists()


class TestResolveSecret:
    def test_env_var_wins(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env_file = tmp_path / "env"
        env_file.write_text("MY_KEY=from-file\n")
        monkeypatch.setenv("MY_KEY", "from-env")
        assert resolve_secret("MY_KEY", env_file) == "from-env"

    def test_falls_back_to_env_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env_file = tmp_path / "env"
        env_file.write_text(
            "# comment\nOTHER=x\nexport MY_KEY=\"from-file\"\n"
        )
        monkeypatch.delenv("MY_KEY", raising=False)
        assert resolve_secret("MY_KEY", env_file) == "from-file"

    def test_missing_everywhere(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("MY_KEY", raising=False)
        assert resolve_secret("MY_KEY", tmp_path / "absent") == ""


class TestWorkerSkipReason:
    def test_skips_when_daemon_running(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "callmem.core.daemon_lock.daemon_running", lambda _p: True
        )
        client = OllamaClient(endpoint="http://localhost:1", model="m")
        assert "daemon" in (_worker_skip_reason(tmp_path, client) or "")

    def test_skips_openai_compat_without_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for var in ("OPENROUTER_KEY", "OPENROUTER_API_KEY",
                    "CALLMEM_API_KEY", "LLM_MEM_API_KEY"):
            monkeypatch.delenv(var, raising=False)
        client = OpenAICompatClient(api_key="")
        assert "API key" in (_worker_skip_reason(tmp_path, client) or "")

    def test_runs_with_key_and_no_daemon(self, tmp_path: Path) -> None:
        client = OpenAICompatClient(api_key="k")
        assert _worker_skip_reason(tmp_path, client) is None
