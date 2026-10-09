"""Tests for daemon startup: pid files, key fallback, worker gating, DNS wait."""

from __future__ import annotations

import os
import socket
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

from callmem.cli import _wait_for_backend_dns
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
from callmem.models.config import Config


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


class TestWaitForBackendDns:
    def _config(self, backend: str, endpoint: str) -> Config:
        return Config.from_dict({
            "llm": {"backend": backend},
            "openai_compat": {"endpoint": endpoint},
            "ollama": {"endpoint": endpoint},
        })

    def test_skips_local_and_ip_endpoints(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _fail(*_a: object) -> None:
            raise AssertionError("should not resolve")

        monkeypatch.setattr(socket, "getaddrinfo", _fail)
        for ep in ("http://localhost:11434", "http://10.200.200.1:11434"):
            assert _wait_for_backend_dns(self._config("ollama", ep)) is True
        assert _wait_for_backend_dns(self._config("none", "")) is True

    def test_waits_until_resolvable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[str] = []

        def _resolve(host: str, *_a: object) -> list[object]:
            calls.append(host)
            if len(calls) < 3:
                raise socket.gaierror("not yet")
            return []

        monkeypatch.setattr(socket, "getaddrinfo", _resolve)
        cfg = self._config("openai_compat", "https://openrouter.ai/api/v1")
        assert _wait_for_backend_dns(cfg, timeout=5, interval=0) is True
        assert calls == ["openrouter.ai"] * 3

    def test_gives_up_after_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _never(*_a: object) -> None:
            raise socket.gaierror("down")

        monkeypatch.setattr(socket, "getaddrinfo", _never)
        cfg = self._config("openai_compat", "https://openrouter.ai/api/v1")
        assert _wait_for_backend_dns(cfg, timeout=0, interval=0) is False
