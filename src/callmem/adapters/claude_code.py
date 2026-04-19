"""Live tailer for Claude Code transcripts.

Polls ``~/.claude/projects/<slug>/*.jsonl`` for this project, keeps a
per-file byte offset, and streams new events into callmem. Mirrors
the behaviour of the OpenCode SSE adapter but for Claude Code's
append-only JSONL format.

Session lifecycle:
    - On the first record in a previously unseen file, open a new
      callmem session tagged with the file's source_id.
    - On every new record, update a ``last_activity`` timestamp.
    - If a file has been idle for ``idle_timeout`` seconds, close its
      session. The next record on that file will open a new session.
    - On shutdown, close every open session cleanly.

Offsets are persisted to ``.callmem/claude_code_offsets.json`` so a
restart picks up where we left off instead of replaying or dropping
records.
"""

from __future__ import annotations

import contextlib
import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from callmem.adapters.claude_code_import import (
    SOURCE_TYPE,
    _detect_model,
    _extract_user_text,
    _find_imported_session_id,
    _map_record,
    claude_project_dir,
)
from callmem.compat import UTC

if TYPE_CHECKING:
    from callmem.core.engine import MemoryEngine

logger = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL = 2.0
DEFAULT_IDLE_TIMEOUT = 300.0  # 5 minutes
OFFSETS_FILE = Path(".callmem") / "claude_code_offsets.json"


class ClaudeCodeAdapter:
    """Polls CC transcripts for this project and ingests new lines."""

    def __init__(
        self,
        engine: MemoryEngine,
        project_path: Path,
        claude_projects_dir: Path | None = None,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        idle_timeout: float = DEFAULT_IDLE_TIMEOUT,
    ) -> None:
        self.engine = engine
        self.project_path = project_path
        self.cc_dir = claude_project_dir(project_path, claude_projects_dir)
        self.poll_interval = poll_interval
        self.idle_timeout = idle_timeout

        self._offsets_path = project_path / OFFSETS_FILE
        self._offsets: dict[str, int] = self._load_offsets()
        # source_id -> (session_id, last_activity_monotonic, title_so_far)
        self._active: dict[str, tuple[str, float, str | None]] = {}
        self._stop_event = threading.Event()

    # ── Public API ────────────────────────────────────────────────

    def run(self) -> None:
        """Poll loop. Blocks until ``stop()`` is called."""
        logger.info(
            "ClaudeCodeAdapter started (dir=%s, poll=%ss)",
            self.cc_dir, self.poll_interval,
        )
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception as exc:  # pragma: no cover — defensive
                logger.warning("ClaudeCodeAdapter tick failed: %s", exc)
            self._stop_event.wait(self.poll_interval)

        # On shutdown, close every session cleanly.
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
            logger.warning("Cannot persist CC offsets: %s", exc)

    # ── Tick ──────────────────────────────────────────────────────

    def _tick(self) -> None:
        if not self.cc_dir.is_dir():
            return

        seen_sources: set[str] = set()
        for jsonl in sorted(self.cc_dir.glob("*.jsonl")):
            source_id = jsonl.stem
            seen_sources.add(source_id)
            self._process_file(jsonl, source_id)

        # Idle timeout: close sessions whose file hasn't grown recently.
        now = time.monotonic()
        for source_id, (_, last_activity, _) in list(self._active.items()):
            if now - last_activity > self.idle_timeout:
                self._close_session(source_id, reason="idle")

        # Save offsets opportunistically.
        self._save_offsets()

    def _process_file(self, jsonl_path: Path, source_id: str) -> None:
        try:
            size = jsonl_path.stat().st_size
        except OSError:
            return

        offset = self._offsets.get(str(jsonl_path), 0)
        if offset > size:
            # File truncated/rotated. Reset to start.
            logger.info("CC transcript %s shrank; restarting", jsonl_path.name)
            offset = 0

        if offset == size:
            return

        try:
            fh = jsonl_path.open("rb")
        except OSError as exc:
            logger.warning("Cannot open %s: %s", jsonl_path, exc)
            return

        try:
            fh.seek(offset)
            raw = fh.read()
            # Only process up to the last full line; stash offset accordingly.
            if b"\n" not in raw:
                return
            last_newline = raw.rfind(b"\n")
            consumed = offset + last_newline + 1
            chunk = raw[: last_newline + 1]
        finally:
            fh.close()

        new_events = 0
        for line in chunk.splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if self._ingest_record(jsonl_path, source_id, record):
                new_events += 1

        self._offsets[str(jsonl_path)] = consumed
        if new_events > 0:
            logger.debug(
                "CC adapter ingested %d event(s) from %s",
                new_events, jsonl_path.name,
            )

    # ── Per-record ingest ─────────────────────────────────────────

    def _ingest_record(
        self, jsonl_path: Path, source_id: str, record: dict[str, Any],
    ) -> bool:
        inputs = _map_record(record)
        user_title = _extract_user_text(record)

        if not inputs and user_title is None:
            # Record was informational-only; still refresh idle timer
            # if we already have a session so a long run of system
            # records doesn't trip the idle close.
            if source_id in self._active:
                sid, _, title = self._active[source_id]
                self._active[source_id] = (sid, time.monotonic(), title)
            return False

        session_id, title = self._ensure_session(jsonl_path, source_id, user_title)
        if not inputs:
            self._active[source_id] = (session_id, time.monotonic(), title)
            return False

        try:
            stored = self.engine.ingest(inputs, session_id=session_id)
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning("CC ingest failed for %s: %s", source_id, exc)
            return False

        self._active[source_id] = (session_id, time.monotonic(), title)
        return bool(stored)

    def _ensure_session(
        self, jsonl_path: Path, source_id: str, user_title: str | None,
    ) -> tuple[str, str | None]:
        if source_id in self._active:
            sid, _, title = self._active[source_id]
            if title is None and user_title:
                title = user_title[:80]
            return sid, title

        # If a previous run already imported this source_id but the
        # session was closed (ended), start a fresh one — new events
        # observed after close should open a continuation session.
        model = _detect_model(jsonl_path)
        existing_id = _find_imported_session_id(self.engine, source_id)
        continuation = existing_id is not None

        session = self.engine.start_session(
            agent_name="claude-code", model_name=model,
        )
        session.metadata = {
            "source_type": SOURCE_TYPE,
            "source_id": source_id,
            "transcript_path": str(jsonl_path),
            "live": True,
            "continuation_of": existing_id if continuation else None,
            "opened_at": datetime.now(UTC).isoformat(),
        }
        self.engine.repo.update_session(session)

        title = user_title[:80] if user_title else None
        self._active[source_id] = (session.id, time.monotonic(), title)
        logger.info(
            "CC session opened: source=%s session=%s continuation=%s",
            source_id, session.id, continuation,
        )
        return session.id, title

    def _close_session(self, source_id: str, reason: str) -> None:
        entry = self._active.pop(source_id, None)
        if entry is None:
            return
        session_id, _, title = entry
        note = title or f"Claude Code {source_id[:8]}"
        with contextlib.suppress(Exception):
            self.engine.end_session(session_id, note=note)
        logger.info(
            "CC session closed: source=%s session=%s reason=%s",
            source_id, session_id, reason,
        )
