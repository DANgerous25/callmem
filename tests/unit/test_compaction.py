"""Tests for memory compaction."""

from __future__ import annotations

from datetime import datetime, timedelta

from callmem.compat import UTC
from typing import TYPE_CHECKING

from callmem.core.compaction import Compactor
from callmem.models.config import Config

if TYPE_CHECKING:
    from callmem.core.database import Database
    pass


def _seed_old_events(memory_db: Database) -> str:
    from callmem.core.repository import Repository
    from callmem.models.events import Event
    from callmem.models.projects import Project
    from callmem.models.sessions import Session
    from callmem.models.summaries import Summary

    repo = Repository(memory_db)
    project = Project(name="test-project")
    repo.create_project(project)

    session = Session(project_id=project.id)
    repo.insert_session(session)

    old_ts = (datetime.now(UTC) - timedelta(days=3)).isoformat()

    e1 = Event(
        session_id=session.id,
        project_id=project.id,
        type="note",
        content="Old event about pagination",
        timestamp=old_ts,
    )
    e2 = Event(
        session_id=session.id,
        project_id=project.id,
        type="note",
        content="Old event about authentication",
        timestamp=old_ts,
    )
    repo.insert_event(e1)
    repo.insert_event(e2)

    summary = Summary(
        project_id=project.id,
        session_id=session.id,
        level="chunk",
        content="Summary covering pagination and auth work",
        event_range_start=old_ts,
        event_range_end=old_ts,
    )
    conn = memory_db.connect()
    try:
        row = summary.to_row()
        conn.execute(
            "INSERT INTO summaries "
            "(id, project_id, session_id, level, content, "
            "event_range_start, event_range_end, event_count, "
            "token_count, created_at, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row["id"], row["project_id"], row["session_id"],
                row["level"], row["content"],
                row["event_range_start"], row["event_range_end"],
                row["event_count"], row["token_count"],
                row["created_at"], row["metadata"],
            ),
        )
        conn.commit()
    finally:
        conn.close()

    return project.id


def _seed_with_entities(memory_db: Database) -> str:
    from callmem.core.repository import Repository
    from callmem.models.entities import Entity
    from callmem.models.events import Event
    from callmem.models.projects import Project
    from callmem.models.sessions import Session
    from callmem.models.summaries import Summary

    repo = Repository(memory_db)
    project = Project(name="test-project")
    repo.create_project(project)

    session = Session(project_id=project.id)
    repo.insert_session(session)

    old_ts = (datetime.now(UTC) - timedelta(days=35)).isoformat()

    e1 = Event(
        session_id=session.id,
        project_id=project.id,
        type="note",
        content="Very old event",
        timestamp=old_ts,
    )
    repo.insert_event(e1)

    summary = Summary(
        project_id=project.id,
        session_id=session.id,
        level="chunk",
        content="Summary of very old work",
        event_range_start=old_ts,
        event_range_end=old_ts,
    )
    conn = memory_db.connect()
    try:
        row = summary.to_row()
        conn.execute(
            "INSERT INTO summaries "
            "(id, project_id, session_id, level, content, "
            "event_range_start, event_range_end, event_count, "
            "token_count, created_at, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row["id"], row["project_id"], row["session_id"],
                row["level"], row["content"],
                row["event_range_start"], row["event_range_end"],
                row["event_count"], row["token_count"],
                row["created_at"], row["metadata"],
            ),
        )
        conn.commit()
    finally:
        conn.close()

    active_todo = Entity(
        project_id=project.id,
        source_event_id=e1.id,
        type="todo",
        title="Active task",
        content="Still needs doing",
        status="open",
        priority="high",
        updated_at=old_ts,
    )
    pinned_fact = Entity(
        project_id=project.id,
        type="fact",
        title="Pinned fact",
        content="Important project knowledge",
        pinned=True,
        updated_at=old_ts,
    )
    old_decision = Entity(
        project_id=project.id,
        type="decision",
        title="Old decision",
        content="Made a while ago",
        updated_at=old_ts,
    )

    for entity in [active_todo, pinned_fact, old_decision]:
        conn = memory_db.connect()
        try:
            row = entity.to_row()
            conn.execute(
                "INSERT INTO entities "
                "(id, project_id, source_event_id, type, title, content, "
                "status, priority, pinned, created_at, updated_at, "
                "resolved_at, metadata, archived_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    row["id"], row["project_id"], row["source_event_id"],
                    row["type"], row["title"], row["content"],
                    row["status"], row["priority"], row["pinned"],
                    row["created_at"], row["updated_at"],
                    row["resolved_at"], row["metadata"], row["archived_at"],
                ),
            )
            conn.commit()
        finally:
            conn.close()

    return project.id


class TestCompactionArchive:
    def test_old_summarized_events_archived(
        self, memory_db: Database
    ) -> None:
        project_id = _seed_old_events(memory_db)
        compactor = Compactor(memory_db, Config())
        stats = compactor.run(project_id)
        assert stats.events_archived > 0

    def test_unsummarized_events_not_archived(
        self, memory_db: Database
    ) -> None:
        from callmem.core.repository import Repository
        from callmem.models.events import Event
        from callmem.models.projects import Project
        from callmem.models.sessions import Session

        repo = Repository(memory_db)
        project = Project(name="test")
        repo.create_project(project)
        session = Session(project_id=project.id)
        repo.insert_session(session)

        old_ts = (datetime.now(UTC) - timedelta(days=5)).isoformat()
        event = Event(
            session_id=session.id,
            project_id=project.id,
            type="note",
            content="Unsummarized old event",
            timestamp=old_ts,
        )
        repo.insert_event(event)

        compactor = Compactor(memory_db, Config())
        stats = compactor.run(project.id)
        assert stats.events_archived == 0

    def test_recent_events_not_archived(
        self, memory_db: Database
    ) -> None:
        from callmem.core.repository import Repository
        from callmem.models.events import Event
        from callmem.models.projects import Project
        from callmem.models.sessions import Session
        from callmem.models.summaries import Summary

        repo = Repository(memory_db)
        project = Project(name="test")
        repo.create_project(project)
        session = Session(project_id=project.id)
        repo.insert_session(session)

        now = datetime.now(UTC).isoformat()
        event = Event(
            session_id=session.id,
            project_id=project.id,
            type="note",
            content="Recent event",
            timestamp=now,
        )
        repo.insert_event(event)

        summary = Summary(
            project_id=project.id,
            session_id=session.id,
            level="chunk",
            content="Recent summary",
            event_range_start=now,
            event_range_end=now,
        )
        conn = memory_db.connect()
        try:
            row = summary.to_row()
            conn.execute(
                "INSERT INTO summaries "
                "(id, project_id, session_id, level, content, "
                "event_range_start, event_range_end, event_count, "
                "token_count, created_at, metadata) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    row["id"], row["project_id"], row["session_id"],
                    row["level"], row["content"],
                    row["event_range_start"], row["event_range_end"],
                    row["event_count"], row["token_count"],
                    row["created_at"], row["metadata"],
                ),
            )
            conn.commit()
        finally:
            conn.close()

        compactor = Compactor(memory_db, Config())
        stats = compactor.run(project.id)
        assert stats.events_archived == 0


class TestCompactionProtection:
    def test_active_todos_survive(self, memory_db: Database) -> None:
        project_id = _seed_with_entities(memory_db)
        compactor = Compactor(memory_db, Config())
        compactor.run(project_id)

        conn = memory_db.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM entities WHERE type = 'todo' AND status = 'open'"
            ).fetchall()
            assert all(r["archived_at"] is None for r in rows)
        finally:
            conn.close()

    def test_pinned_entities_survive(self, memory_db: Database) -> None:
        project_id = _seed_with_entities(memory_db)
        compactor = Compactor(memory_db, Config())
        compactor.run(project_id)

        conn = memory_db.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM entities WHERE pinned = 1"
            ).fetchall()
            assert all(r["archived_at"] is None for r in rows)
        finally:
            conn.close()

    def test_old_unprotected_entities_archived(
        self, memory_db: Database
    ) -> None:
        project_id = _seed_with_entities(memory_db)
        compactor = Compactor(memory_db, Config())
        stats = compactor.run(project_id)
        assert stats.entities_archived > 0


def _seed_entity_with_status(
    memory_db: Database,
    project_id: str,
    entity_type: str,
    status: str | None,
    updated_at: str,
) -> str:
    """Insert a single old entity of `entity_type`/`status` for compaction
    protection tests. Returns the entity id."""
    from callmem.models.entities import Entity

    entity = Entity(
        project_id=project_id,
        type=entity_type,  # type: ignore[arg-type]
        title=f"{entity_type} entity",
        content="Test content",
        status=status,  # type: ignore[arg-type]
        updated_at=updated_at,
    )
    conn = memory_db.connect()
    try:
        row = entity.to_row()
        conn.execute(
            "INSERT INTO entities "
            "(id, project_id, source_event_id, type, title, content, "
            "status, priority, pinned, created_at, updated_at, "
            "resolved_at, metadata, archived_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row["id"], row["project_id"], row["source_event_id"],
                row["type"], row["title"], row["content"],
                row["status"], row["priority"], row["pinned"],
                row["created_at"], row["updated_at"],
                row["resolved_at"], row["metadata"], row["archived_at"],
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return entity.id


class TestCompactionStatusVocabulary:
    """Compaction must protect ANY non-closed lifecycle status, not just
    the todo type's 'open' — the same vocabulary-gap bug class as the
    mem_reopen fix. Statuses: done/cancelled/resolved are closed; open and
    unresolved are open lifecycle states; NULL means no lifecycle at all.
    """

    def _run_and_fetch(self, memory_db: Database, entity_id: str) -> str | None:
        conn = memory_db.connect()
        try:
            row = conn.execute(
                "SELECT archived_at FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
            return row["archived_at"]
        finally:
            conn.close()

    def test_old_unresolved_failure_survives(self, memory_db: Database) -> None:
        project_id = _seed_old_events(memory_db)
        old_ts = (datetime.now(UTC) - timedelta(days=40)).isoformat()
        entity_id = _seed_entity_with_status(
            memory_db, project_id, "failure", "unresolved", old_ts
        )
        compactor = Compactor(memory_db, Config())
        compactor.run(project_id)
        assert self._run_and_fetch(memory_db, entity_id) is None

    def test_old_open_todo_survives(self, memory_db: Database) -> None:
        project_id = _seed_old_events(memory_db)
        old_ts = (datetime.now(UTC) - timedelta(days=40)).isoformat()
        entity_id = _seed_entity_with_status(
            memory_db, project_id, "todo", "open", old_ts
        )
        compactor = Compactor(memory_db, Config())
        compactor.run(project_id)
        assert self._run_and_fetch(memory_db, entity_id) is None

    def test_old_done_todo_archived(self, memory_db: Database) -> None:
        project_id = _seed_old_events(memory_db)
        old_ts = (datetime.now(UTC) - timedelta(days=40)).isoformat()
        entity_id = _seed_entity_with_status(
            memory_db, project_id, "todo", "done", old_ts
        )
        compactor = Compactor(memory_db, Config())
        compactor.run(project_id)
        assert self._run_and_fetch(memory_db, entity_id) is not None

    def test_old_status_null_fact_archived(self, memory_db: Database) -> None:
        project_id = _seed_old_events(memory_db)
        old_ts = (datetime.now(UTC) - timedelta(days=40)).isoformat()
        entity_id = _seed_entity_with_status(
            memory_db, project_id, "fact", None, old_ts
        )
        compactor = Compactor(memory_db, Config())
        compactor.run(project_id)
        assert self._run_and_fetch(memory_db, entity_id) is not None


def _seed_multi_event_todo(memory_db: Database) -> tuple[str, str, str]:
    """Two old, summarized events; a todo whose source_event_id is the
    first but whose source_event_ids covers both. Returns
    (project_id, first_event_id, second_event_id)."""
    from callmem.core.repository import Repository
    from callmem.models.entities import Entity
    from callmem.models.events import Event
    from callmem.models.projects import Project
    from callmem.models.sessions import Session
    from callmem.models.summaries import Summary

    repo = Repository(memory_db)
    project = Project(name="test-project")
    repo.create_project(project)

    session = Session(project_id=project.id)
    repo.insert_session(session)

    old_ts = (datetime.now(UTC) - timedelta(days=3)).isoformat()

    e1 = Event(
        session_id=session.id, project_id=project.id,
        type="note", content="First event", timestamp=old_ts,
    )
    e2 = Event(
        session_id=session.id, project_id=project.id,
        type="note", content="Second event", timestamp=old_ts,
    )
    repo.insert_event(e1)
    repo.insert_event(e2)

    summary = Summary(
        project_id=project.id, session_id=session.id, level="chunk",
        content="Summary covering both events",
        event_range_start=old_ts, event_range_end=old_ts,
    )
    conn = memory_db.connect()
    try:
        row = summary.to_row()
        conn.execute(
            "INSERT INTO summaries "
            "(id, project_id, session_id, level, content, "
            "event_range_start, event_range_end, event_count, "
            "token_count, created_at, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row["id"], row["project_id"], row["session_id"],
                row["level"], row["content"],
                row["event_range_start"], row["event_range_end"],
                row["event_count"], row["token_count"],
                row["created_at"], row["metadata"],
            ),
        )
        conn.commit()
    finally:
        conn.close()

    todo = Entity(
        project_id=project.id,
        source_event_id=e1.id,
        source_event_ids=[e1.id, e2.id],
        type="todo",
        title="Multi-event task",
        content="Spans two events",
        status="open",
        priority="high",
        updated_at=old_ts,
    )
    conn = memory_db.connect()
    try:
        row = todo.to_row()
        conn.execute(
            "INSERT INTO entities "
            "(id, project_id, source_event_id, source_event_ids, type, "
            "title, content, status, priority, pinned, created_at, "
            "updated_at, resolved_at, metadata, archived_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row["id"], row["project_id"], row["source_event_id"],
                row["source_event_ids"], row["type"], row["title"],
                row["content"], row["status"], row["priority"],
                row["pinned"], row["created_at"], row["updated_at"],
                row["resolved_at"], row["metadata"], row["archived_at"],
            ),
        )
        conn.commit()
    finally:
        conn.close()

    return project.id, e1.id, e2.id


class TestCompactionProtectsFullEventList:
    def test_protection_covers_non_first_source_event(
        self, memory_db: Database
    ) -> None:
        """An open todo's source_event_ids may include events beyond the
        first — all of them must be protected from archival, not just
        source_event_id (phase0-reliability task 6)."""
        project_id, first_id, second_id = _seed_multi_event_todo(memory_db)
        compactor = Compactor(memory_db, Config())
        compactor.run(project_id)

        conn = memory_db.connect()
        try:
            row = conn.execute(
                "SELECT archived_at FROM events WHERE id = ?", (second_id,)
            ).fetchone()
            assert row["archived_at"] is None, (
                "second event referenced only via source_event_ids "
                "was archived despite the open todo protecting it"
            )
            row = conn.execute(
                "SELECT archived_at FROM events WHERE id = ?", (first_id,)
            ).fetchone()
            assert row["archived_at"] is None
        finally:
            conn.close()


class TestCompactionLog:
    def test_log_created(self, memory_db: Database) -> None:
        project_id = _seed_old_events(memory_db)
        compactor = Compactor(memory_db, Config())
        compactor.run(project_id)

        conn = memory_db.connect()
        try:
            rows = conn.execute("SELECT * FROM compaction_log").fetchall()
            assert len(rows) == 1
            assert rows[0]["events_archived"] > 0
            assert rows[0]["duration_ms"] >= 0
            assert rows[0]["policy_config"] is not None
        finally:
            conn.close()


class TestCompactionSearch:
    def test_archived_excluded_from_default_search(
        self, memory_db: Database
    ) -> None:
        project_id = _seed_old_events(memory_db)
        compactor = Compactor(memory_db, Config())
        compactor.run(project_id)

        from callmem.core.retrieval import RetrievalEngine

        engine = RetrievalEngine(
            __import__(
                "callmem.core.repository", fromlist=["Repository"]
            ).Repository(memory_db),
            Config(),
        )
        results = engine.search(project_id, "pagination")
        assert len(results) == 0

    def test_archived_included_with_flag(
        self, memory_db: Database
    ) -> None:
        project_id = _seed_old_events(memory_db)
        compactor = Compactor(memory_db, Config())
        compactor.run(project_id)

        from callmem.core.retrieval import RetrievalEngine

        engine = RetrievalEngine(
            __import__(
                "callmem.core.repository", fromlist=["Repository"]
            ).Repository(memory_db),
            Config(),
        )
        results = engine.search(
            project_id, "pagination", include_archived=True
        )
        assert len(results) > 0
