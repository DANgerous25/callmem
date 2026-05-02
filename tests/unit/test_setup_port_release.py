"""Regression tests for setup wizard port reclaim behaviour.

A rerun of setup must be able to reclaim the port its own daemon is
currently using. The previous 1-second sleep after ``systemctl stop``
was not always enough for the socket to actually release, which made
``port_available()`` report the port as in-use and caused setup to
drift upward through unrelated ports.
"""

from __future__ import annotations

import socket
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pytest

from callmem import setup_wizard as setup  # noqa: E402


def _bound_socket() -> tuple[socket.socket, int]:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    return s, s.getsockname()[1]


class TestPortAvailable:
    def test_free_port_is_available(self) -> None:
        s, port = _bound_socket()
        s.close()
        assert setup.port_available(port, host="127.0.0.1")

    def test_bound_port_is_not_available(self) -> None:
        s, port = _bound_socket()
        try:
            assert not setup.port_available(port, host="127.0.0.1")
        finally:
            s.close()


class TestWaitPortFree:
    def test_returns_true_when_port_is_free(self) -> None:
        s, port = _bound_socket()
        s.close()
        assert setup._wait_port_free(port, host="127.0.0.1", timeout=2.0)

    def test_returns_true_after_socket_releases(self) -> None:
        s, port = _bound_socket()

        def _release_after_delay() -> None:
            time.sleep(0.3)
            s.close()

        t = threading.Thread(target=_release_after_delay, daemon=True)
        t.start()
        assert setup._wait_port_free(port, host="127.0.0.1", timeout=3.0)
        t.join(timeout=1)

    def test_returns_false_on_timeout(self) -> None:
        s, port = _bound_socket()
        try:
            assert not setup._wait_port_free(
                port, host="127.0.0.1", timeout=0.3,
            )
        finally:
            s.close()


class TestStopOwnServiceWaitsForPort:
    def test_calls_wait_port_free_when_port_given(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: dict[str, object] = {}

        # Pretend a unit file exists.
        unit_dir = tmp_path / "systemd"
        unit_dir.mkdir()
        project = tmp_path / "my-project"
        project.mkdir()
        svc_name = f"callmem-{project.name}"
        (unit_dir / f"{svc_name}.service").write_text("# fake")

        monkeypatch.setattr(
            Path, "home", lambda: tmp_path,  # redirects ~/.config path
            raising=True,
        )
        # The unit path is ~/.config/systemd/user, not ~/systemd — wire it up.
        (tmp_path / ".config").mkdir()
        (tmp_path / ".config" / "systemd").mkdir()
        (tmp_path / ".config" / "systemd" / "user").mkdir()
        (tmp_path / ".config" / "systemd" / "user" / f"{svc_name}.service"
        ).write_text("# fake")

        import subprocess

        def _fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess:
            calls.setdefault("cmds", []).append(cmd)
            if "is-active" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout="active\n", stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", _fake_run)

        def _fake_wait(port: int, host: str = "0.0.0.0", timeout: float = 10.0) -> bool:
            calls["wait_port"] = port
            calls["wait_host"] = host
            return True

        monkeypatch.setattr(setup, "_wait_port_free", _fake_wait)

        result = setup._stop_own_service(project, port=9090)
        assert result == svc_name
        assert calls.get("wait_port") == 9090
