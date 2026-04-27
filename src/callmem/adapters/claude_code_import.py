"""Claude Code session transcript importer.

Reads JSONL files from ``~/.claude/projects/<slug>/`` and ingests
historical Claude Code sessions into callmem. Counterpart to
``opencode_import.py`` but for a different on-disk format.

Claude Code stores each session as a single JSONL file. Each line is
a JSON object with a ``type`` field. The relevant types for memory
ingestion are:

* ``user`` — user message (or a tool-result from the system)
* ``assistant`` — assistant reply (text / tool_use blocks)

Everything else (``permission-mode``, ``attachment``, ``last-prompt``,
``file-history-snapshot``, ``system/*``) is skipped in the MVP.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
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

CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"

PROGRESS_FILE = Path(".callmem") / "claude_code_import_progress.json"
LOCK_FILE = Path(".callmem") / "claude_code_import.lock"

SOURCE_TYPE = "claude-code"

# Record types we skip wholesale. "system" is a prefix — any record
# whose type starts with "system" is considered bookkeeping.
_SKIP_TYPES = frozenset({
    "permission-mode",
    "attachment",
    "last-prompt",
    "file-history-snapshot",
})


def project_slug(project_path: Path) -> str:
    """Derive the Claude Code project slug from a worktree path.

    CC encodes an absolute path by replacing ``/`` with ``-``, so
    ``/home/user/my-project`` becomes ``-home-user-my-project``.
    """
    return str(project_path.resolve()).replace("/", "-")


def claude_project_dir(
    project_path: Path, root: Path | None = None
) -> Path:
    """Return the ``~/.claude/projects/<slug>`` directory for this worktree."""
    base = root if root is not None else CLAUDE_PROJECTS_DIR
    return base / project_slug(project_path)


# ── Locking + progress ──────────────────────────────────────────────


def _progress_path(project: Path) -> Path:
    return project / PROGRESS_FILE


def _lock_path(project: Path) -> Path:
    return project / LOCK_FILE


def read_import_progress(project: Path) -> dict[str, Any]:
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
    lock = _lock_path(project)
    lock.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock), os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, BlockingIOError):
        os.close(fd)
        raise RuntimeError(
            "Another Claude Code import is already in progress."
        ) from None
    return fd


def _release_lock(fd: Any) -> None:
    with contextlib.suppress(OSError):
        fcntl.flock(fd, fcntl.LOCK_UN)
    with contextlib.suppress(OSError):
        os.close(fd)


# ── Discovery ───────────────────────────────────────────────────────


def discover_sessions(
    project_path: Path,
    claude_projects_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """Return a summary for every CC transcript belonging to this project."""
    cc_dir = claude_project_dir(project_path, claude_projects_dir)
    if not cc_dir.is_dir():
        return []

    sessions: list[dict[str, Any]] = []
    for jsonl in sorted(cc_dir.glob("*.jsonl")):
        summary = _summarize_jsonl(jsonl)
        if summary is not None:
            sessions.append(summary)
    return sessions


def _summarize_jsonl(jsonl: Path) -> dict[str, Any] | None:
    first_ts: str | None = None
    last_ts: str | None = None
    message_count = 0
    title: str | None = None
    try:
        with jsonl.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = record.get("timestamp")
                if ts:
                    if first_ts is None:
                        first_ts = ts
                    last_ts = ts
                rtype = record.get("type")
                if rtype in ("user", "assistant"):
                    message_count += 1
                if title is None and rtype == "user":
                    title = _extract_user_text(record)
    except OSError as exc:
        logger.warning("Cannot read %s: %s", jsonl, exc)
        return None

    if message_count == 0 and first_ts is None:
        return None

    return {
        "id": jsonl.stem,
        "title": (title or f"Claude Code {jsonl.stem[:8]}")[:80],
        "message_count": message_count,
        "first_timestamp": first_ts,
        "last_timestamp": last_ts,
        "path": str(jsonl),
    }


# ── Mapping ─────────────────────────────────────────────────────────


def _extract_user_text(record: dict[str, Any]) -> str | None:
    """Return the user message text if the record is a real user prompt."""
    if record.get("isMeta"):
        return None
    msg = record.get("message") or {}
    content = msg.get("content")
    if not isinstance(content, str):
        return None
    stripped = content.strip()
    if not stripped:
        return None
    if stripped.startswith("<local-command") or stripped.startswith("<command-"):
        return None
    return stripped


def _truncate(value: str, limit: int = 200) -> str:
    return value if len(value) <= limit else value[:limit]


def _map_record(record: dict[str, Any]) -> list[EventInput]:
    """Translate one transcript record into zero or more EventInputs."""
    rtype = record.get("type", "")
    if not rtype or rtype in _SKIP_TYPES or rtype.startswith("system"):
        return []

    ts = record.get("timestamp")
    msg = record.get("message") or {}

    if rtype == "user":
        if record.get("isMeta"):
            return []
        content = msg.get("content")
        if isinstance(content, str):
            stripped = content.strip()
            if not stripped:
                return []
            if stripped.startswith("<local-command") or stripped.startswith("<command-"):
                # Slash-command invocations: keep, helps trace what ran
                return [EventInput(type="prompt", content=stripped, timestamp=ts)]
            return [EventInput(type="prompt", content=stripped, timestamp=ts)]
        # tool_result list content — no dedicated EventType yet; skip
        return []

    if rtype == "assistant":
        content = msg.get("content")
        if isinstance(content, str) and content:
            return [EventInput(type="response", content=content, timestamp=ts)]
        if not isinstance(content, list):
            return []

        out: list[EventInput] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                text = block.get("text", "")
                if text:
                    out.append(EventInput(type="response", content=text, timestamp=ts))
            elif btype == "tool_use":
                name = block.get("name", "unknown")
                args = block.get("input", {})
                if isinstance(args, dict):
                    try:
                        args_str = json.dumps(args)
                    except (TypeError, ValueError):
                        args_str = str(args)
                else:
                    args_str = str(args)
                content_str = f"{name}({_truncate(args_str)})" if args_str else name
                out.append(EventInput(
                    type="tool_call", content=content_str, timestamp=ts,
                ))
            # thinking blocks intentionally skipped for MVP
        return out

    return []


def _detect_model(jsonl_path: Path) -> str | None:
    """Return the assistant model name from the first assistant record."""
    try:
        fh = jsonl_path.open(encoding="utf-8")
    except OSError:
        return None
    with fh:
        for line in fh:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("type") == "assistant":
                model = (record.get("message") or {}).get("model")
                return model if isinstance(model, str) else None
    return None


# ── Existing-session lookup (idempotency) ───────────────────────────


def _find_imported_session_id(
    engine: MemoryEngine, source_id: str
) -> str | None:
    """Return the callmem session id previously imported from this CC file."""
    conn = engine.db.connect()
    try:
        cursor = conn.execute(
            "SELECT id, metadata FROM sessions "
            "WHERE project_id = ? AND agent_name = ? "
            "ORDER BY started_at DESC",
            (engine.project_id, "claude-code"),
        )
        for row in cursor.fetchall():
            raw = row["metadata"]
            if not raw:
                continue
            try:
                meta = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            if (
                meta.get("source_type") == SOURCE_TYPE
                and meta.get("source_id") == source_id
            ):
                return str(row["id"])
    finally:
        conn.close()
    return None


# ── Per-session import ──────────────────────────────────────────────


def import_session(
    engine: MemoryEngine,
    jsonl_path: Path,
) -> dict[str, Any]:
    """Replay a single CC transcript into callmem.

    Idempotent: if a session with the same source_id has already been
    imported for this project, returns immediately with ``skipped=True``.
    """
    source_id = jsonl_path.stem

    existing_id = _find_imported_session_id(engine, source_id)
    if existing_id is not None:
        return {
            "source_id": source_id,
            "session_id": existing_id,
            "event_count": 0,
            "skipped": True,
        }

    model_name = _detect_model(jsonl_path)
    session = engine.start_session(
        agent_name="claude-code", model_name=model_name,
    )
    session.metadata = {
        "source_type": SOURCE_TYPE,
        "source_id": source_id,
        "transcript_path": str(jsonl_path),
    }
    engine.repo.update_session(session)

    event_count = 0
    errors: list[str] = []
    title: str | None = None

    try:
        with jsonl_path.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if title is None:
                    candidate = _extract_user_text(record)
                    if candidate:
                        title = candidate[:80]
                try:
                    inputs = _map_record(record)
                    if inputs:
                        stored = engine.ingest(inputs, session_id=session.id)
                        event_count += len(stored)
                except Exception as exc:  # pragma: no cover — defensive
                    errors.append(str(exc))
                    logger.warning("Error processing record: %s", exc)
    except OSError as exc:
        errors.append(f"Failed to read transcript: {exc}")

    try:
        engine.end_session(
            session.id, note=title or f"Claude Code {source_id[:8]}",
        )
    except Exception as exc:  # pragma: no cover — defensive
        errors.append(f"Failed to end session: {exc}")
        logger.warning("Failed to end session %s: %s", session.id, exc)

    return {
        "source_id": source_id,
        "session_id": session.id,
        "event_count": event_count,
        "errors": errors,
        "skipped": False,
    }


# ── Batch import ────────────────────────────────────────────────────


def import_sessions(
    engine: MemoryEngine,
    project_path: Path,
    claude_projects_dir: Path | None = None,
    progress_callback: ProgressCallback | None = None,
    project: Path | None = None,
    dry_run: bool = False,
) -> list[dict[str, Any]]:
    """Import every CC transcript for this worktree."""
    sessions = discover_sessions(project_path, claude_projects_dir)

    if progress_callback is not None:
        progress_callback({
            "phase": "discovery",
            "total_sessions": len(sessions),
            "total_events_estimate": sum(
                s.get("message_count", 0) for s in sessions
            ),
        })

    if dry_run:
        return [{**s, "dry_run": True} for s in sessions]

    if not sessions:
        return []

    lock_fd = None
    if project is not None:
        _write_progress(project, {
            "pid": os.getpid(),
            "started_at": datetime.now(UTC).isoformat(),
            "total_sessions": len(sessions),
            "imported_sessions": 0,
            "status": "running",
        })
        lock_fd = _acquire_lock(project)

    results: list[dict[str, Any]] = []
    imported_events = 0
    try:
        for idx, summary in enumerate(sessions, start=1):
            result = import_session(engine, Path(summary["path"]))
            results.append(result)
            imported_events += result.get("event_count", 0)

            if progress_callback is not None:
                progress_callback({
                    "phase": "importing",
                    "session_index": idx,
                    "total_sessions": len(sessions),
                    "session_title": summary.get("title", ""),
                    "session_events": result.get("event_count", 0),
                    "total_events_so_far": imported_events,
                    "skipped": result.get("skipped", False),
                })

            if project is not None:
                _write_progress(project, {
                    "pid": os.getpid(),
                    "total_sessions": len(sessions),
                    "imported_sessions": idx,
                    "imported_events": imported_events,
                    "status": "running",
                })
    finally:
        if lock_fd is not None:
            _release_lock(lock_fd)

    if project is not None:
        _write_progress(project, {
            "pid": os.getpid(),
            "total_sessions": len(sessions),
            "imported_sessions": len(results),
            "imported_events": imported_events,
            "status": "completed",
            "completed_at": datetime.now(UTC).isoformat(),
        })

    return results
