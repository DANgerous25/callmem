"""Core memory engine — the central coordinator for all memory operations.

All adapters (MCP, REST, direct Python) route through this class.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from llm_mem.core.repository import Repository
from llm_mem.models.events import Event, EventInput, EventType
from llm_mem.models.projects import Project
from llm_mem.models.sessions import Session

if TYPE_CHECKING:
    from llm_mem.core.database import Database
    from llm_mem.models.config import Config

TRUNCATION_MARKER = "\n\n[... truncated at {max_chars} chars]"
DEFAULT_MAX_EVENT_SIZE = 100_000
DEFAULT_DEDUP_WINDOW_S = 60


class MemoryEngine:
    """Central coordinator for all memory operations.

    Usage:
        db = Database(".llm-mem/memory.db")
        db.initialize()
        config = Config()
        engine = MemoryEngine(db, config)

        session = engine.start_session(agent_name="opencode")
        events = engine.ingest([EventInput(type="prompt", content="...")])
        engine.end_session(session.id)
    """

    def __init__(self, db: Database, config: Config) -> None:
        self.db = db
        self.config = config
        self.repo = Repository(db)
        self._project_id: str | None = None

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

        from datetime import UTC, datetime

        session.ended_at = datetime.now(UTC).isoformat()
        session.status = "ended"
        if note is not None:
            session.summary = note
        self.repo.update_session(session)
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

    def ingest(self, events: list[EventInput]) -> list[Event]:
        """Ingest a batch of events into the current session.

        If no active session exists and auto_start is enabled,
        a new session is created automatically.
        """
        if not events:
            return []

        session = self._ensure_active_session()

        stored: list[Event] = []
        for inp in events:
            event = self._create_event(session, inp)
            if event is not None:
                stored.append(event)

        if stored:
            self._update_session_event_count(session, len(stored))

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

    # ── Private helpers ──────────────────────────────────────────────

    def _ensure_active_session(self) -> Session:
        """Return the active session or create one if auto_start is on."""
        session = self.get_active_session()
        if session is not None:
            return session
        return self.start_session()

    def _create_event(self, session: Session, inp: EventInput) -> Event | None:
        """Create and store a single event, applying dedup and truncation."""
        content = self._truncate_content(inp.content)

        if self._is_duplicate(session.project_id, inp.type, content):
            return None

        event = Event(
            session_id=session.id,
            project_id=session.project_id,
            type=inp.type,
            content=content,
            metadata=inp.metadata,
        )
        self.repo.insert_event(event)
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
