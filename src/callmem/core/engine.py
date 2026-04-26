"""Core memory engine — the central coordinator for all memory operations.

All adapters (MCP, REST, direct Python) route through this class.
"""

from __future__ import annotations

import fnmatch
import logging
from typing import TYPE_CHECKING, Any

from callmem.core.repository import Repository
from callmem.models.events import Event, EventInput, EventType
from callmem.models.projects import Project
from callmem.models.sessions import Session

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from callmem.core.database import Database
    from callmem.core.redaction import Detection
    from callmem.models.config import Config

TRUNCATION_MARKER = "\n\n[... truncated at {max_chars} chars]"
DEFAULT_MAX_EVENT_SIZE = 100_000
DEFAULT_DEDUP_WINDOW_S = 60


def _create_llm_client(config: Config) -> Any:
    """Create the appropriate LLM client based on config.

    Returns an object with is_available(), extract(), and scan_sensitive() methods,
    or None if backend is 'none'.
    """
    backend = config.llm.backend

    if backend == "ollama":
        from callmem.core.ollama import OllamaClient

        return OllamaClient(
            endpoint=config.ollama.endpoint,
            model=config.ollama.model,
            timeout=config.ollama.timeout,
            num_ctx=config.ollama.num_ctx,
        )

    if backend == "openai_compat":
        import os

        from callmem.core.openai_compat import OpenAICompatClient

        api_key = os.environ.get(config.openai_compat.api_key_env, "")
        return OpenAICompatClient(
            endpoint=config.openai_compat.endpoint,
            model=config.openai_compat.model,
            api_key=api_key,
            timeout=config.openai_compat.timeout,
        )

    # backend == "none"
    return None


class MemoryEngine:
    """Central coordinator for all memory operations.

    Usage:
        db = Database(".callmem/memory.db")
        db.initialize()
        config = Config()
        engine = MemoryEngine(db, config)

        session = engine.start_session(agent_name="opencode")
        events = engine.ingest([EventInput(type="prompt", content="...")])
        engine.end_session(session.id)
    """

    def __init__(
        self,
        db: Database,
        config: Config,
        event_bus: Any | None = None,
    ) -> None:
        self.db = db
        self.config = config
        self.repo = Repository(db)
        self._project_id: str | None = None
        self.event_bus = event_bus

        if config.sensitive_data.enabled:
            from callmem.core.redaction import PatternScanner

            self.pattern_scanner = PatternScanner()
        else:
            self.pattern_scanner = None

        self.llm_client = _create_llm_client(config)
        # Backwards compat — workers and briefing reference self.ollama
        self.ollama = self.llm_client

        from callmem.core.queue import JobQueue

        self.queue = JobQueue(db)
        self._ingestion_stats: dict[str, int] = {"skipped_tool_calls": 0}
        self._file_context_stats: dict[str, int] = {
            "calls": 0,
            "hits": 0,
            "misses": 0,
        }

    @property
    def project_id(self) -> str:
        """Lazily resolve or create the project for this engine."""
        if self._project_id is not None:
            return self._project_id

        project_name = self.config.project.name or "default"
        existing = self.repo.get_project_by_name(project_name)
        if existing is not None:
            self._project_id = existing.id
            return self._project_id

        project = Project(name=project_name)
        self.repo.create_project(project)
        self._project_id = project.id
        return self._project_id

    # ── Session management ───────────────────────────────────────────

    def start_session(
        self,
        agent_name: str | None = None,
        model_name: str | None = None,
    ) -> Session:
        """Create a new active session."""
        session = Session(
            project_id=self.project_id,
            agent_name=agent_name,
            model_name=model_name,
        )
        self.repo.insert_session(session)
        self._publish("session_started", {
            "id": session.id,
            "started_at": session.started_at,
            "agent_name": session.agent_name,
        })
        return session

    def end_session(
        self, session_id: str, note: str | None = None
    ) -> Session:
        """End an active session."""
        session = self.repo.get_session(session_id)
        if session is None:
            msg = f"Session not found: {session_id}"
            raise ValueError(msg)
        if session.status != "active":
            msg = f"Session {session_id} is not active (status={session.status})"
            raise ValueError(msg)

        from datetime import datetime

        from callmem.compat import UTC

        session.ended_at = datetime.now(UTC).isoformat()
        session.status = "ended"
        if note is not None:
            session.summary = note
        self.repo.update_session(session)

        self._publish("session_ended", {
            "id": session.id,
            "ended_at": session.ended_at,
            "summary": session.summary,
        })

        self.queue.enqueue(
            "generate_summary",
            {
                "level": "session",
                "project_id": session.project_id,
                "session_id": session.id,
            },
        )
        self._maybe_queue_cross_session_summary(session.project_id)

        if self.config.compaction.enabled:
            self.queue.enqueue(
                "compact",
                {"project_id": session.project_id},
            )

        return session

    def get_active_session(self) -> Session | None:
        """Return the current active session, if any."""
        return self.repo.get_active_session(self.project_id)

    def get_session(self, session_id: str) -> Session | None:
        """Get a session by ID."""
        return self.repo.get_session(session_id)

    def list_sessions(
        self, limit: int = 20, offset: int = 0
    ) -> list[Session]:
        """List sessions for this project."""
        return self.repo.list_sessions(self.project_id, limit, offset)

    # ── Ingest ───────────────────────────────────────────────────────

    def ingest(
        self, events: list[EventInput], session_id: str | None = None
    ) -> list[Event]:
        """Ingest a batch of events into the current session.

        If no active session exists and auto_start is enabled,
        a new session is created automatically. If session_id is
        provided, events are attached to that session directly
        (bypasses _ensure_active_session).
        """
        if not events:
            return []

        if session_id is not None:
            session = self.get_session(session_id)
            if session is None:
                raise ValueError(f"Session {session_id} not found")
        else:
            session = self._ensure_active_session()

        stored: list[Event] = []
        for inp in events:
            if self._should_skip_tool_call(inp):
                self._ingestion_stats["skipped_tool_calls"] += 1
                continue
            event = self._create_event(session, inp)
            if event is not None:
                stored.append(event)

        if stored:
            self._update_session_event_count(session, len(stored))
            self.queue.enqueue(
                "extract_entities",
                {
                    "event_ids": [e.id for e in stored],
                    "session_id": session.id,
                },
            )
            self._maybe_queue_chunk_summary(session, stored)

        return stored

    def ingest_one(
        self,
        type: EventType,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> Event | None:
        """Ingest a single event. Convenience wrapper around ingest()."""
        results = self.ingest([EventInput(type=type, content=content, metadata=metadata)])
        return results[0] if results else None

    # ── Read ─────────────────────────────────────────────────────────

    def get_events(
        self,
        session_id: str | None = None,
        type: EventType | None = None,
        limit: int = 50,
    ) -> list[Event]:
        """Retrieve events, optionally filtered by session and type."""
        return self.repo.get_events(
            self.project_id, session_id=session_id, type=type, limit=limit
        )

    def get_event(self, event_id: str) -> Event | None:
        """Get a single event by ID."""
        return self.repo.get_event(event_id)

    # ── Search & entities ────────────────────────────────────────────

    def search(
        self,
        query: str,
        types: list[str] | None = None,
        session_id: str | None = None,
        limit: int = 20,
        include_stale: bool = False,
    ) -> list[dict[str, Any]]:
        """Search memories using the retrieval engine."""
        from callmem.core.retrieval import RetrievalEngine

        engine = RetrievalEngine(self.repo, self.config)
        results = engine.search(
            self.project_id,
            query,
            types=types,
            session_id=session_id,
            limit=limit,
            include_stale=include_stale,
        )
        return [
            {
                "id": r.id,
                "source_type": r.source_type,
                "type": r.type,
                "title": r.title,
                "content": r.content,
                "score": round(r.score, 4),
                "timestamp": r.timestamp,
                "session_id": r.session_id,
                "key_points": r.key_points,
                "synopsis": r.synopsis,
                "extracted_by": r.extracted_by,
                "status": r.status,
                "priority": r.priority,
                "pinned": r.pinned,
                "stale": r.stale,
            }
            for r in results
        ]

    def get_briefing(
        self,
        max_tokens: int | None = None,
        focus: str | None = None,
    ) -> dict[str, Any]:
        """Generate a startup briefing for the current project."""
        from callmem.core.briefing import BriefingGenerator

        gen = BriefingGenerator(self.repo, self.config, self.ollama)
        project_name = self.config.project.name or "default"
        briefing = gen.generate(
            self.project_id,
            project_name=project_name,
            max_tokens=max_tokens,
            focus=focus,
        )
        return {
            "project_name": briefing.project_name,
            "content": briefing.content,
            "token_count": briefing.token_count,
            "components": briefing.components,
            "observations_loaded": briefing.observations_loaded,
            "read_tokens": briefing.read_tokens,
            "work_investment": briefing.work_investment,
            "savings_pct": briefing.savings_pct,
        }

    def search_fts(
        self, query: str, project_id: str | None = None, limit: int = 20
    ) -> list[dict[str, Any]]:
        """Search events using FTS5 full-text search."""
        pid = project_id or self.project_id
        return self.repo.search_events_fts(pid, query, limit)

    def get_entities(
        self,
        type: str | None = None,
        status: str | None = None,
        limit: int = 50,
        include_stale: bool = False,
    ) -> list[dict[str, Any]]:
        """Retrieve entities, optionally filtered by type and status."""
        return self.repo.get_entities(
            self.project_id, type=type, status=status, limit=limit,
            include_stale=include_stale,
        )

    def set_pinned(self, entity_id: str, pinned: bool = True) -> dict[str, Any]:
        """Toggle the pinned status of an entity."""
        return self.repo.set_pinned(entity_id, pinned)

    def mark_stale(
        self, entity_id: str, reason: str,
        superseded_by: str | None = None,
    ) -> dict[str, Any] | None:
        """Flag an entity as stale. Returns the updated entity row."""
        changed = self.repo.mark_stale(entity_id, reason, superseded_by)
        if not changed:
            return self.repo.get_entity(entity_id)
        self._publish("entity_marked_stale", {
            "entity_id": entity_id,
            "reason": reason,
            "superseded_by": superseded_by,
        })
        return self.repo.get_entity(entity_id)

    def mark_current(self, entity_id: str) -> dict[str, Any] | None:
        """Clear the stale flag. Returns the updated entity row."""
        changed = self.repo.mark_current(entity_id)
        if changed:
            self._publish("entity_marked_current", {"entity_id": entity_id})
        return self.repo.get_entity(entity_id)

    def list_stale_entities(self, limit: int = 200) -> list[dict[str, Any]]:
        return self.repo.list_stale_entities(self.project_id, limit=limit)

    def mark_false_positive(self, vault_id: str) -> dict[str, Any]:
        """Mark a vault entry as false positive and un-redact the event content."""
        from callmem.core.crypto import VaultKeyManager

        vault_entry = self.repo.get_vault_entry(vault_id)
        if vault_entry is None:
            msg = f"Vault entry not found: {vault_id}"
            raise ValueError(msg)

        if vault_entry["false_positive"]:
            return vault_entry

        event_id = vault_entry["event_id"]
        if not event_id:
            self.repo.mark_vault_false_positive(vault_id)
            return self.repo.get_vault_entry(vault_id)

        event = self.repo.get_event(event_id)
        if event is None:
            self.repo.mark_vault_false_positive(vault_id)
            return self.repo.get_vault_entry(vault_id)

        vault_dir = self.db.db_path.parent
        crypto = VaultKeyManager(vault_dir)
        original_value = crypto.decrypt(vault_entry["ciphertext"])
        redaction_token = f"[REDACTED:{vault_entry['category']}:{vault_id}]"
        unredacted = event.content.replace(redaction_token, original_value)
        event.content = unredacted
        self.repo.update_event_content(event.id, event.content)

        self.repo.mark_vault_false_positive(vault_id)

        return self.repo.get_vault_entry(vault_id)

    # ── Private helpers ──────────────────────────────────────────────

    def _publish(self, event_type: str, data: dict[str, Any]) -> None:
        """Publish an event to the SSE event bus if available."""
        if self.event_bus is not None:
            self.event_bus.publish(event_type, data)

    def ingestion_stats(self) -> dict[str, int]:
        """Return a copy of the running ingestion counters."""
        return dict(self._ingestion_stats)

    def file_context_stats(self) -> dict[str, int]:
        """Return a copy of the file-read-gate counters."""
        return dict(self._file_context_stats)

    def check_context(
        self,
        message_count: int,
        estimated_tokens: int = 0,
    ) -> dict[str, Any]:
        """Advise the agent on whether to compress older context.

        The model-side context window is not visible from here, so this
        is a recommendation: if the agent's own estimate (messages or
        tokens) crosses the configured threshold, return
        ``compress_recommended``; otherwise ``ok``.
        """
        cfg = self.config.endless_mode
        context_limit = cfg.context_limit
        if context_limit is None:
            context_limit = self.config.ollama.num_ctx
        if context_limit is None:
            context_limit = 128_000

        threshold = cfg.compress_threshold

        # Prefer the agent's own token estimate; fall back to a
        # coarse 500 tok/msg heuristic if only message_count was given.
        TOKENS_PER_MESSAGE = 500
        effective_tokens = (
            estimated_tokens if estimated_tokens > 0
            else message_count * TOKENS_PER_MESSAGE
        )
        usage = (
            min(1.0, effective_tokens / context_limit)
            if context_limit > 0 else 0.0
        )

        base = {
            "enabled": cfg.enabled,
            "context_limit": context_limit,
            "compress_threshold": threshold,
            "chunk_size": cfg.chunk_size,
            "message_count": message_count,
            "estimated_tokens": estimated_tokens,
            "effective_tokens": effective_tokens,
            "usage_ratio": round(usage, 3),
        }

        if not cfg.enabled:
            base["status"] = "disabled"
            base["action"] = (
                "Endless mode disabled — no compression recommended."
            )
            return base

        if usage >= threshold:
            free_hint = cfg.chunk_size * TOKENS_PER_MESSAGE
            base["status"] = "compress_recommended"
            base["reason"] = (
                f"Session has {message_count} messages "
                f"(~{effective_tokens} tokens, "
                f"{int(usage * 100)}% of the {context_limit}-token "
                f"window), above the "
                f"{int(threshold * 100)}% threshold. Compressing the "
                f"oldest {cfg.chunk_size} messages will free context."
            )
            base["action"] = (
                "Summarize the oldest ~{n} messages and call "
                "mem_compress_context with the summary."
            ).format(n=cfg.chunk_size)
            base["recommended_chunk_size"] = cfg.chunk_size
            base["free_tokens_hint"] = free_hint
            return base

        base["status"] = "ok"
        base["action"] = (
            "Context is under the compression threshold. Continue."
        )
        return base

    def compress_context(
        self,
        summary: str,
        message_range: str = "",
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Store an agent-provided context summary as a session milestone.

        Persists the summary as a cross-session-style ``summary`` record
        scoped to the active (or given) session and updates the session
        metadata counter. Returns a marker the agent should drop into
        its conversation in place of the compressed exchanges.
        """
        if not summary.strip():
            raise ValueError("summary is required")

        if session_id is None:
            session = self.get_active_session()
            if session is None:
                raise ValueError(
                    "No active session — call mem_session_start first.",
                )
            session_id = session.id
        else:
            session = self.get_session(session_id)
            if session is None:
                raise ValueError(f"Session {session_id} not found")

        import json
        from datetime import datetime

        from ulid import ULID

        from callmem.compat import UTC

        now = datetime.now(UTC).isoformat()
        summary_id = str(ULID())
        meta_blob = json.dumps(
            {"source": "endless_mode", "message_range": message_range or None},
        )

        self.repo.insert_summary(
            id=summary_id,
            project_id=session.project_id,
            session_id=session_id,
            level="chunk",
            content=summary,
            event_range_start=message_range or None,
            event_range_end=message_range or None,
            event_count=None,
            token_count=len(summary) // 4,
            created_at=now,
            metadata=meta_blob,
        )

        metadata = dict(session.metadata or {})
        compressions = int(metadata.get("compression_events", 0)) + 1
        metadata["compression_events"] = compressions
        metadata["last_compression_at"] = now
        session.metadata = metadata
        self.repo.update_session(session)

        marker = (
            f"[Context compressed — range={message_range or 'oldest'} "
            f"summarized by callmem. Use mem_search to recall details.]"
        )

        self._publish("context_compressed", {
            "session_id": session_id,
            "summary_id": summary_id,
            "compression_events": compressions,
        })

        return {
            "status": "compressed",
            "session_id": session_id,
            "summary_id": summary_id,
            "compression_events": compressions,
            "marker": marker,
            "message_range": message_range,
        }

    def get_file_context(
        self, path: str, include_content: bool = False,
    ) -> dict[str, Any]:
        """Return the observation timeline callmem has for ``path``.

        When ``has_observations`` is False the agent should read the
        file normally; otherwise the timeline is often enough to skip
        the raw read.
        """
        self._file_context_stats["calls"] += 1

        rows = self.repo.get_file_timeline(path)

        if not rows:
            self._file_context_stats["misses"] += 1
            result: dict[str, Any] = {
                "path": path,
                "has_observations": False,
                "observation_count": 0,
                "timeline": [],
                "recommendation": (
                    "No prior observations — read the file normally."
                ),
            }
            if include_content:
                result["current_content"] = self._read_file_safely(path)
            return result

        self._file_context_stats["hits"] += 1

        timeline_cap = 20
        timeline = [
            {
                "id": r["id"],
                "date": (r.get("created_at") or "")[:10],
                "type": r.get("type"),
                "title": r.get("title") or "",
                "summary": r.get("synopsis") or r.get("title") or "",
            }
            for r in rows[:timeline_cap]
        ]
        truncated = len(rows) - timeline_cap if len(rows) > timeline_cap else 0

        first_seen = (rows[0].get("created_at") or "")[:10]
        last_modified = (rows[-1].get("created_at") or "")[:10]
        latest = rows[-1]
        current_state = (
            latest.get("synopsis") or latest.get("key_points")
            or latest.get("title") or ""
        )

        result = {
            "path": path,
            "has_observations": True,
            "observation_count": len(rows),
            "first_seen": first_seen,
            "last_modified": last_modified,
            "timeline": timeline,
            "timeline_truncated": truncated,
            "current_state": current_state,
            "recommendation": (
                "Timeline covers recorded changes. Raw read only "
                "needed for exact line-level details."
            ),
        }
        if include_content:
            result["current_content"] = self._read_file_safely(path)
        return result

    @staticmethod
    def _read_file_safely(path: str) -> str | None:
        """Best-effort file read. Returns None if the file can't be opened."""
        from pathlib import Path

        try:
            p = Path(path)
            if not p.is_file():
                return None
            return p.read_text(encoding="utf-8", errors="replace")
        except (OSError, UnicodeDecodeError):
            return None

    def _should_skip_tool_call(self, inp: EventInput) -> bool:
        """Return True if this event matches the configured tool filter.

        Only ``tool_call`` events are eligible — other event types
        (prompt, response, etc.) are always ingested.
        """
        if inp.type != "tool_call":
            return False

        skip_tools = self.config.ingestion.skip_tools
        skip_patterns = self.config.ingestion.skip_patterns
        if not skip_tools and not skip_patterns:
            return False

        content = inp.content or ""
        tool_name = content.split("(", 1)[0].strip()

        if tool_name and tool_name in skip_tools:
            logger.debug("Skipped tool call: %s (matches skip_tools)", tool_name)
            return True

        for pattern in skip_patterns:
            if fnmatch.fnmatchcase(content, pattern):
                logger.debug(
                    "Skipped tool call: %s (matches skip_patterns=%r)",
                    tool_name or content[:40], pattern,
                )
                return True

        return False

    def _ensure_active_session(self) -> Session:
        """Return the active session or create one if auto_start is on."""
        session = self.get_active_session()
        if session is not None:
            return session
        return self.start_session()

    def _create_event(self, session: Session, inp: EventInput) -> Event | None:
        """Create and store a single event, applying dedup, redaction, and truncation."""
        content = self._truncate_content(inp.content)

        if self._is_duplicate(session.project_id, inp.type, content):
            return None

        detections: list[Detection] = []
        scan_status = "none"

        if self.pattern_scanner is not None:
            from callmem.core.redaction import apply_redactions, merge_detections

            detections = self.pattern_scanner.scan(content)
            scan_status = "pattern_only"

            if self.llm_client is not None and self.llm_client.is_available():
                llm_detections = self.llm_client.scan_sensitive(content)
                confidence = self.config.sensitive_data.llm_scan_confidence
                llm_detections = [
                    d for d in llm_detections if d.confidence >= confidence
                ]
                detections = merge_detections(detections, llm_detections)
                scan_status = "full"

            if detections:
                content = apply_redactions(content, detections)

        metadata = dict(inp.metadata) if inp.metadata else {}
        metadata["scan_status"] = scan_status

        event_kwargs: dict[str, Any] = {
            "session_id": session.id,
            "project_id": session.project_id,
            "type": inp.type,
            "content": content,
            "metadata": metadata,
        }
        if inp.timestamp is not None:
            event_kwargs["timestamp"] = inp.timestamp
        event = Event(**event_kwargs)
        self.repo.insert_event(event)

        if detections:
            from callmem.core.crypto import VaultKeyManager

            vault_dir = self.db.db_path.parent
            crypto = VaultKeyManager(vault_dir)
            for d in detections:
                ciphertext = crypto.encrypt(d.original_value)
                self.repo.insert_vault_entry(
                    id=d.vault_id,
                    project_id=session.project_id,
                    category=d.category,
                    detector=d.detector,
                    pattern_name=d.pattern_name,
                    ciphertext=ciphertext,
                    event_id=event.id,
                )

        return event

    def _truncate_content(self, content: str) -> str:
        """Truncate content exceeding the configured max size."""
        max_chars = DEFAULT_MAX_EVENT_SIZE
        if len(content) <= max_chars:
            return content
        marker = TRUNCATION_MARKER.format(max_chars=max_chars)
        return content[:max_chars] + marker

    def _is_duplicate(
        self, project_id: str, event_type: str, content: str
    ) -> bool:
        """Check if an identical event was recently ingested."""
        existing = self.repo.find_recent_event(
            project_id,
            content,
            event_type,
            DEFAULT_DEDUP_WINDOW_S,
        )
        return existing is not None

    def _update_session_event_count(
        self, session: Session, added: int
    ) -> None:
        """Increment the session's event_count and persist."""
        session.event_count += added
        self.repo.update_session(session)

    def _maybe_queue_chunk_summary(
        self, session: Session, new_events: list[Event]
    ) -> None:
        """Queue a chunk summary if the session event count hits the threshold."""
        chunk_size = self.config.summarization.chunk_size
        if chunk_size <= 0:
            return

        if session.event_count % chunk_size != 0:
            return

        event_ids = self.repo.get_session_event_ids_for_summary(
            session.id, chunk_size, session.event_count - chunk_size,
        )
        if not event_ids:
            return

        self.queue.enqueue(
            "generate_summary",
            {
                "level": "chunk",
                "event_ids": event_ids,
                "project_id": session.project_id,
                "session_id": session.id,
            },
        )

    def _maybe_queue_cross_session_summary(
        self, project_id: str
    ) -> None:
        """Queue a cross-session summary every N ended sessions."""
        interval = self.config.summarization.cross_session_interval
        if interval <= 0:
            return

        ended_count = self.repo.count_ended_sessions(project_id)

        if ended_count % interval != 0:
            return

        self.queue.enqueue(
            "generate_summary",
            {
                "level": "cross_session",
                "project_id": project_id,
                "max_tokens": self.config.briefing.max_tokens,
            },
        )
