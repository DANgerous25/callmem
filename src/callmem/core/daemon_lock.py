"""Pid-file registry so other processes can tell a project's daemon is up.

The MCP server spawned by an agent would otherwise start a second set of
queue workers. Those workers run in the agent's environment — which often
lacks the backend API key the daemon gets from its systemd drop-in — and
their startup reaper resets jobs the daemon is mid-way through. When the
daemon is alive, the MCP server leaves the queue to it.

Pid files live outside the project (under ``~/.local/state``) so they never
show up as untracked files in the project's git status.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

STATE_DIR = Path.home() / ".local" / "state" / "callmem" / "daemons"


def pid_file_path(project_path: Path, state_dir: Path | None = None) -> Path:
    """Return the pid-file path for a project's daemon."""
    key = hashlib.sha1(str(project_path.resolve()).encode()).hexdigest()[:16]
    return (state_dir or STATE_DIR) / f"{key}.pid"


def write_pid_file(project_path: Path, state_dir: Path | None = None) -> Path:
    """Record the current process as the project's daemon."""
    path = pid_file_path(project_path, state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{os.getpid()}\n")
    return path


def remove_pid_file(project_path: Path, state_dir: Path | None = None) -> None:
    """Remove the pid file if it still belongs to this process."""
    path = pid_file_path(project_path, state_dir)
    if _read_pid(path) == os.getpid():
        path.unlink(missing_ok=True)


def daemon_running(project_path: Path, state_dir: Path | None = None) -> bool:
    """True if a live callmem process holds the project's daemon pid file."""
    pid = _read_pid(pid_file_path(project_path, state_dir))
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass  # alive but owned by another user; the cmdline check decides
    # Guard against pid reuse after a crash left a stale file behind.
    cmdline = Path(f"/proc/{pid}/cmdline")
    if cmdline.exists():
        return b"callmem" in cmdline.read_bytes()
    return True


def _read_pid(path: Path) -> int | None:
    """Parse a pid file, returning None if it's missing or malformed."""
    try:
        return int(path.read_text().strip())
    except (FileNotFoundError, ValueError):
        return None
