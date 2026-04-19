"""Tests for memory compaction."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
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
