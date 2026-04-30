"""Live tailer for OpenCode sessions via its SQLite database.

Polls ``~/.local/share/opencode/opencode.db`` for new sessions and
messages belonging to the current project. Tracks a per-session cursor
so it only ingests messages newer than the last processed timestamp.

Session lifecycle:
    - When a new session with messages appears, start a callmem session.
    - When a session has been idle (no new messages) for the idle timeout,
      end the callmem session.
    - On shutdown, close all open sessions cleanly.

Cursors are persisted to ``.callmem/opencode_db_offset.json`` so a
restart picks up where it left off.
"""

from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from callmem.adapters.opencode_import import (
    DEFAULT_DB_PATH,
    _connect_readonly,
    _load_session_messages,
    _map_message,
)

if TYPE_CHECKING:
    from callmem.core.engine import MemoryEngine

logger = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL: float = 3.0
DEFAULT_IDLE_TIMEOUT: float = 300.0  # 5 minutes
OFFSETS_FILE: Path = Path(".callmem") / "opencode_db_offset.json"


def _find_project_sessions(
    conn: sqlite3.Connection,
    project_path: Path,
) -> list[dict[str, Any]]:
    """Find all sessions in the OpenCode DB that belong to the project."""
    resolved_project = str(project_path.resolve()).rstrip("/")
    rows = conn.execute(
        """
        SELECT id, title, time_created, directory
        FROM session
        WHERE directory IS NOT NULL
        ORDER BY time_created
        """
    ).fetchall()

    sessions: list[dict[str, Any]] = []
    for row in rows:
        directory = (row["directory"] or "").rstrip("/")
        try:
            if Path(directory).resolve() != Path(resolved_project).resolve():
                continue
        except (OSError, ValueError):
            continue
        sessions.append({
            "id": row["id"],
            "title": row["title"] or "",
            "time_created": row["time_created"],
        })
    return sessions


def _get_latest_message_ts(
    conn: sqlite3.Connection,
    session_id: str,
) -> int | None:
    """Get the max time_created for messages in a session."""
    row = conn.execute(
        "SELECT MAX(time_created) FROM message WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    return row[0] if row and row[0] is not None else None


class OpenCodeDBAdapter:
    """Polls OpenCode's SQLite database for new session messages."""

    def __init__(
        self,
        engine: MemoryEngine,
        project_path: Path,
        db_path: Path | None = None,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        idle_timeout: float = DEFAULT_IDLE_TIMEOUT,
    ) -> None:
        self.engine = engine
        self.project_path = project_path.resolve()
        self.db_path = db_path or DEFAULT_DB_PATH
        self.poll_interval = poll_interval
        self.idle_timeout = idle_timeout

        self._offsets_path = self.project_path / OFFSETS_FILE
        self._offsets: dict[str, int] = self._load_offsets()
        self._active: dict[str, tuple[str, float, str | None]] = {}
        self._stop_event = threading.Event()

    # ── Public API ────────────────────────────────────────────────

    def run(self) -> None:
        """Poll loop. Blocks until ``stop()`` is called."""
        logger.info(
            "OpenCodeDBAdapter started (db=%s, poll=%ss)",
            self.db_path, self.poll_interval,
        )
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception as exc:
                logger.warning("OpenCodeDBAdapter tick failed: %s", exc)
            self._stop_event.wait(self.poll_interval)

        for source_id in list(self._active):
            self._close_session(source_id, reason="adapter-stop")
        self._save_offsets()

    def stop(self) -> None:
        self._stop_event.set()

    # ── Offsets ───────────────────────────────────────────────────

    def _load_offsets(self) -> dict[str, int]:
        if not self._offsets_path.exists():
            return {}
        try:
            data = json.loads(self._offsets_path.read_text())
            return {k: int(v) for k, v in data.items() if isinstance(v, (int, float))}
        except (json.JSONDecodeError, OSError, ValueError):
            return {}

    def _save_offsets(self) -> None:
        try:
            self._offsets_path.parent.mkdir(parents=True, exist_ok=True)
            self._offsets_path.write_text(json.dumps(self._offsets, indent=2))
        except OSError as exc:
            logger.warning("Cannot persist OpenCode DB offsets: %s", exc)

    # ── Tick ──────────────────────────────────────────────────────

    def _tick(self) -> None:
        if not self.db_path.is_file():
            return

        try:
            conn = _connect_readonly(self.db_path)
        except sqlite3.Error as exc:
            logger.warning("Cannot open OpenCode DB: %s", exc)
            return

        try:
            sessions = _find_project_sessions(conn, self.project_path)
            seen_sessions: set[str] = set()
            for session in sessions:
                session_id = session["id"]
                seen_sessions.add(session_id)
                self._process_session(conn, session_id, session)
        finally:
            conn.close()

        now = time.monotonic()
        for source_id, (_, last_activity, _) in list(self._active.items()):
            if now - last_activity > self.idle_timeout:
                self._close_session(source_id, reason="idle")

        self._save_offsets()

    def _process_session(
        self,
        conn: sqlite3.Connection,
        session_id: str,
        session_data: dict[str, Any],
    ) -> None:
        latest_ts = _get_latest_message_ts(conn, session_id)
        if latest_ts is None:
            return

        cursor = self._offsets.get(session_id)
        if cursor is None:
            self._offsets[session_id] = latest_ts
            return
        if latest_ts <= cursor:
            # Already caught up; just refresh the idle timer
            if session_id in self._active:
                sid, _, title = self._active[session_id]
                self._active[session_id] = (sid, time.monotonic(), title)
            return

        messages = _load_session_messages(conn, session_id)
        new_messages = [
            m for m in messages
            if m.get("timestamp") is not None
            and self._ts_to_ms(m["timestamp"]) > cursor
        ]

        if not new_messages:
            self._offsets[session_id] = latest_ts
            if session_id in self._active:
                sid, _, title = self._active[session_id]
                self._active[session_id] = (sid, time.monotonic(), title)
            return

        sid, title = self._ensure_session(session_id, session_data.get("title"))
        ingested = 0
        for msg in new_messages:
            inputs = _map_message(msg)
            if not inputs:
                continue
            try:
                stored = self.engine.ingest(inputs, session_id=sid)
                ingested += len(stored)
            except Exception as exc:
                logger.warning(
                    "OpenCode DB ingest failed for session %s: %s",
                    session_id, exc,
                )

        self._offsets[session_id] = latest_ts
        self._active[session_id] = (sid, time.monotonic(), title)

    def _ts_to_ms(self, ts: str | None) -> int:
        if ts is None:
            return 0
        ts_str = ts.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(ts_str)
            return int(dt.timestamp() * 1000)
        except (ValueError, TypeError):
            return 0

    def _ensure_session(
        self, session_id: str, title: str = "",
    ) -> tuple[str, str | None]:
        if session_id in self._active:
            sid, _, existing_title = self._active[session_id]
            if not existing_title and title:
                existing_title = title[:80]
            return sid, existing_title

        session = self.engine.start_session(agent_name="opencode")
        session_title = title[:80] if title else title
        self._active[session_id] = (session.id, time.monotonic(), session_title)
        logger.info(
            "OpenCode DB session opened: source=%s session=%s",
            session_id, session.id,
        )
        return session.id, session_title

    def _close_session(self, source_id: str, reason: str) -> None:
        entry = self._active.pop(source_id, None)
        if entry is None:
            return
        session_id, _, title = entry
        note = title or f"OpenCode {source_id[:8]}"
        with contextlib.suppress(Exception):
            self.engine.end_session(session_id, note=note)
        logger.info(
            "OpenCode DB session closed: source=%s session=%s reason=%s",
            source_id, session_id, reason,
        )