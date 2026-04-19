"""OpenCode session history importer.

Reads OpenCode sessions from its SQLite database and ingests them
into callmem as historical sessions. Separate from the live SSE adapter.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import sqlite3
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from callmem.compat import UTC
from callmem.models.events import EventInput

if TYPE_CHECKING:
    from callmem.core.engine import MemoryEngine

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[dict[str, Any]], None]


def _default_db_path() -> Path:
    """Return the default path to the OpenCode SQLite database."""
    xdg = os.environ.get("XDG_DATA_HOME", "")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "opencode" / "opencode.db"


DEFAULT_DB_PATH = _default_db_path()

# Keep the old name around for anything that imports it
DEFAULT_SESSION_DIR = DEFAULT_DB_PATH.parent


PROGRESS_FILE = ".callmem" / Path("import_progress.json")
LOCK_FILE = ".callmem" / Path("import.lock")


def _progress_path(project: Path) -> Path:
    return project / PROGRESS_FILE


def _lock_path(project: Path) -> Path:
    return project / LOCK_FILE


def read_import_progress(project: Path) -> dict[str, Any]:
    """Read the current import progress for a project.

    Returns the progress dict or empty dict if no progress file exists.
    """
    p = _progress_path(project)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text())
        if data.get("status") == "running" and data.get("pid"):
            try:
                os.kill(data["pid"], 0)
            except (ProcessLookupError, PermissionError):
                data["status"] = "stale"
                p.write_text(json.dumps(data, indent=2))
        return data
    except (json.JSONDecodeError, OSError):
        return {}


def _write_progress(project: Path, progress: dict[str, Any]) -> None:
    p = _progress_path(project)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(progress, indent=2))


def _acquire_lock(project: Path) -> Any:
    """Acquire the import lockfile. Raises RuntimeError if already held."""
    lock = _lock_path(project)
    lock.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock), os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, BlockingIOError):
        os.close(fd)
        raise RuntimeError(
            "Another import is already in progress. "
            "Use 'callmem import --status' to check progress."
        ) from None
    return fd


def _release_lock(fd: Any) -> None:
    import contextlib

    with contextlib.suppress(OSError):
        fcntl.flock(fd, fcntl.LOCK_UN)
    with contextlib.suppress(OSError):
        os.close(fd)


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    """Open a read-only connection to an SQLite database."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def discover_sessions(
    db_path: Path | None = None,
    project_path: str | None = None,
) -> list[dict[str, Any]]:
    """Query the OpenCode DB and return a list of session summaries.

    Each summary dict has: id, title, project_id, project_name,
    project_worktree, message_count, time_created.

    Args:
        db_path: Path to the OpenCode SQLite database.
        project_path: If given, only return sessions whose project
            worktree matches this path (resolved for comparison).
    """
    if db_path is None:
        db_path = DEFAULT_DB_PATH
    if not db_path.is_file():
        logger.warning("OpenCode database not found at %s", db_path)
        return []

    try:
        conn = _connect_readonly(db_path)
    except sqlite3.Error as exc:
        logger.warning("Cannot open OpenCode database: %s", exc)
        return []

    try:
        query = """
            SELECT
                s.id            AS session_id,
                s.title         AS title,
                s.project_id    AS project_id,
                p.name          AS project_name,
                p.worktree      AS project_worktree,
                s.time_created  AS time_created,
                (SELECT COUNT(*) FROM message m WHERE m.session_id = s.id)
                    AS message_count
            FROM session s
            JOIN project p ON p.id = s.project_id
            ORDER BY s.time_created
        """
        rows = conn.execute(query).fetchall()

        sessions: list[dict[str, Any]] = []
        for row in rows:
            worktree = row["project_worktree"] or ""
            if project_path is not None:
                try:
                    if Path(worktree).resolve() != Path(project_path).resolve():
                        continue
                except (OSError, ValueError):
                    continue
            sessions.append({
                "id": row["session_id"],
                "title": row["title"],
                "project_id": row["project_id"],
                "project_name": row["project_name"],
                "project_worktree": worktree,
                "message_count": row["message_count"],
                "time_created": row["time_created"],
            })
        return sessions
    except sqlite3.Error as exc:
        logger.warning("Error querying OpenCode sessions: %s", exc)
        return []
    finally:
        conn.close()


def _load_session_messages(
    conn: sqlite3.Connection,
    session_id: str,
) -> list[dict[str, Any]]:
    """Load all messages + parts for a session, ordered chronologically.

    Returns a list of message dicts with keys: role, content, tool_calls,
    parts (file changes), timestamp.
    """
    msg_rows = conn.execute(
        """
        SELECT id, data, time_created
        FROM message
        WHERE session_id = ?
        ORDER BY time_created, id
        """,
        (session_id,),
    ).fetchall()

    messages: list[dict[str, Any]] = []
    for msg_row in msg_rows:
        msg_data = _parse_json(msg_row["data"])
        role = msg_data.get("role", "")

        # Load parts for this message
        part_rows = conn.execute(
            """
            SELECT data
            FROM part
            WHERE message_id = ?
            ORDER BY id
            """,
            (msg_row["id"],),
        ).fetchall()

        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        file_changes: list[dict[str, Any]] = []

        for part_row in part_rows:
            part_data = _parse_json(part_row["data"])
            part_type = part_data.get("type", "")

            if part_type == "text":
                text = part_data.get("text", part_data.get("content", ""))
                if text:
                    text_parts.append(text)
            elif part_type == "tool-invocation" or part_type == "tool_call":
                name = part_data.get("toolName", part_data.get("name", "unknown"))
                args = part_data.get("args", part_data.get("input", ""))
                if isinstance(args, dict):
                    args = json.dumps(args)[:200]
                elif isinstance(args, str) and len(args) > 200:
                    args = args[:200]
                tool_calls.append({"name": name, "args": args})
            elif part_type == "file_change":
                path = part_data.get("path", "unknown")
                change = part_data.get("change", "modified")
                file_changes.append({"type": "file_change", "path": path, "change": change})

        ts_ms = msg_row["time_created"]
        ts_iso = datetime.fromtimestamp(ts_ms / 1000, tz=UTC).isoformat() if ts_ms else None

        messages.append({
            "role": role,
            "content": "\n".join(text_parts),
            "tool_calls": tool_calls,
            "file_changes": file_changes,
            "timestamp": ts_iso,
        })

    return messages


def _parse_json(raw: str | None) -> dict[str, Any]:
    """Safely parse a JSON string into a dict."""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _map_message(message: dict[str, Any]) -> list[EventInput]:
    """Map a single reconstructed message to callmem EventInput(s)."""
    events: list[EventInput] = []
    role = message.get("role", "")
    content = message.get("content", "")
    ts = message.get("timestamp")

    if content:
        if role == "user":
            events.append(EventInput(type="prompt", content=content, timestamp=ts))
        elif role == "assistant":
            events.append(EventInput(type="response", content=content, timestamp=ts))

    for tc in message.get("tool_calls", []):
        name = tc.get("name", "unknown")
        args = tc.get("args", "")
        tc_content = f"{name}({args})" if args else name
        events.append(EventInput(type="tool_call", content=tc_content, timestamp=ts))

    for fc in message.get("file_changes", []):
        path = fc.get("path", "unknown")
        change = fc.get("change", "modified")
        events.append(EventInput(type="file_change", content=f"{change}: {path}", timestamp=ts))

    return events


def import_session(
    engine: MemoryEngine,
    session_data: dict[str, Any],
    messages: list[dict[str, Any]],
) -> dict[str, Any]:
    """Import a single OpenCode session into callmem.

    Args:
        engine: The MemoryEngine instance.
        session_data: Session metadata (id, title, etc).
        messages: Pre-loaded message dicts from _load_session_messages.

    Returns:
        A summary dict with session_id, event_count, and any errors.
    """
    title = session_data.get("title", "imported session")

    session = engine.start_session(agent_name="opencode")

    event_count = 0
    errors: list[str] = []

    for msg in messages:
        try:
            inputs = _map_message(msg)
            if inputs:
                stored = engine.ingest(inputs, session_id=session.id)
                event_count += len(stored)
        except Exception as exc:
            errors.append(str(exc))
            logger.warning("Error processing message: %s", exc)

    try:
        engine.end_session(session.id, note=title)
    except Exception as exc:
        errors.append(f"Failed to end session: {exc}")
        logger.warning("Failed to end session %s: %s", session.id, exc)

    return {
        "session_id": session.id,
        "source_id": session_data.get("id", "unknown"),
        "title": title,
        "event_count": event_count,
        "errors": errors,
    }


def import_sessions(
    engine: MemoryEngine,
    db_path: Path | None = None,
    session_id: str | None = None,
    project_path: str | None = None,
    import_all: bool = False,
    dry_run: bool = False,
    progress_callback: ProgressCallback | None = None,
    project: Path | None = None,
) -> list[dict[str, Any]]:
    """Import OpenCode sessions from the SQLite database.

    Args:
        engine: The MemoryEngine instance.
        db_path: Path to the OpenCode SQLite database.
        session_id: If provided, only import the session matching this ID.
        project_path: If provided, only import sessions for this project worktree.
        import_all: If True, import all discovered sessions.
        dry_run: If True, report what would be imported without importing.
        progress_callback: Optional callable receiving progress dicts during import.
        project: Project root path (for progress file and lockfile).

    Returns:
        List of result dicts, one per session processed.
    """
    if db_path is None:
        db_path = DEFAULT_DB_PATH

    sessions = discover_sessions(db_path=db_path, project_path=project_path)
    if not sessions:
        return []

    if session_id is not None:
        sessions = [s for s in sessions if s["id"] == session_id]

    if not sessions:
        return []

    if dry_run or (not import_all and session_id is None):
        return [
            {
                "source_id": s["id"],
                "title": s["title"],
                "message_count": s["message_count"],
                "project_name": s.get("project_name", ""),
                "project_worktree": s.get("project_worktree", ""),
                "dry_run": True,
            }
            for s in sessions
        ]

    if not db_path.is_file():
        return []

    total_sessions = len(sessions)
    total_events_estimate = sum(s.get("message_count", 0) for s in sessions)

    if progress_callback is not None:
        progress_callback({
            "phase": "discovery",
            "total_sessions": total_sessions,
            "total_events_estimate": total_events_estimate,
        })

    if project is not None:
        _write_progress(project, {
            "pid": os.getpid(),
            "started_at": datetime.now(UTC).isoformat(),
            "total_sessions": total_sessions,
            "imported_sessions": 0,
            "total_events": total_events_estimate,
            "imported_events": 0,
            "jobs_queued": 0,
            "status": "running",
        })

    try:
        conn = _connect_readonly(db_path)
    except sqlite3.Error as exc:
        logger.warning("Cannot open OpenCode database for import: %s", exc)
        return []

    lock_fd = None
    if project is not None:
        try:
            lock_fd = _acquire_lock(project)
        except RuntimeError:
            conn.close()
            raise

    results: list[dict[str, Any]] = []
    imported_sessions = 0
    imported_events = 0
    try:
        for i, s in enumerate(sessions):
            messages = _load_session_messages(conn, s["id"])
            result = import_session(engine, s, messages)
            results.append(result)
            imported_sessions += 1
            imported_events += result.get("event_count", 0)

            if progress_callback is not None:
                progress_callback({
                    "phase": "importing",
                    "session_index": i + 1,
                    "total_sessions": total_sessions,
                    "session_title": s.get("title", ""),
                    "session_events": result.get("event_count", 0),
                    "total_events_so_far": imported_events,
                })

            if project is not None:
                _write_progress(project, {
                    "pid": os.getpid(),
                    "started_at": _read_started_at(project),
                    "total_sessions": total_sessions,
                    "imported_sessions": imported_sessions,
                    "total_events": total_events_estimate,
                    "imported_events": imported_events,
                    "jobs_queued": 0,
                    "status": "running",
                })
    finally:
        conn.close()
        if lock_fd is not None:
            _release_lock(lock_fd)

    if project is not None:
        _write_progress(project, {
            "pid": os.getpid(),
            "started_at": _read_started_at(project),
            "total_sessions": total_sessions,
            "imported_sessions": imported_sessions,
            "total_events": total_events_estimate,
            "imported_events": imported_events,
            "jobs_queued": 0,
            "status": "completed",
            "completed_at": datetime.now(UTC).isoformat(),
        })

    return results


def _read_started_at(project: Path) -> str:
    data = read_import_progress(project)
    return data.get("started_at", datetime.now(UTC).isoformat())
