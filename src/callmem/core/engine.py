"""Core memory engine — the central coordinator for all memory operations.

All adapters (MCP, REST, direct Python) route through this class.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from callmem.core.repository import Repository
from callmem.models.events import Event, EventInput, EventType
from callmem.models.projects import Project
from callmem.models.sessions import Session

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
        self, query: str, limit: int = 20
    ) -> list[dict[str, Any]]:
        """Search events using FTS5 full-text search."""
        conn = self.db.connect()
        try:
            rows = conn.execute(
                "SELECT e.id, e.type, e.content, e.timestamp, e.session_id "
                "FROM events_fts f "
                "JOIN events e ON e.rowid = f.rowid "
                "WHERE events_fts MATCH ? "
                "ORDER BY rank LIMIT ?",
                (query, limit),
            ).fetchall()
            return [
                {
                    "id": r["id"],
                    "type": r["type"],
                    "content": r["content"],
                    "timestamp": r["timestamp"],
                    "session_id": r["session_id"],
                }
                for r in rows
            ]
        finally:
            conn.close()

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

        conn = self.db.connect()
        try:
            rows = conn.execute(
                "SELECT id FROM events "
                "WHERE session_id = ? ORDER BY timestamp ASC "
                "LIMIT ? OFFSET ?",
                (session.id, chunk_size, session.event_count - chunk_size),
            ).fetchall()
        finally:
            conn.close()

        event_ids = [r["id"] for r in rows]
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

        conn = self.db.connect()
        try:
            row = conn.execute(
                "SELECT COUNT(*) as c FROM sessions "
                "WHERE project_id = ? AND status = 'ended'",
                (project_id,),
            ).fetchone()
            ended_count = row["c"]
        finally:
            conn.close()

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
