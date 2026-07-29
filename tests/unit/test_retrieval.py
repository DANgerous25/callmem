"""Tests for the retrieval engine."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from callmem.core.retrieval import RetrievalEngine, _recency_factor

if TYPE_CHECKING:
    from callmem.core.database import Database
    pass


def _seed_data(memory_db: Database) -> str:
    from callmem.core.repository import Repository
    from callmem.models.entities import Entity
    from callmem.models.events import Event
    from callmem.models.projects import Project
    from callmem.models.sessions import Session

    repo = Repository(memory_db)

    project = Project(name="test-project")
    repo.create_project(project)

    session = Session(project_id=project.id)
    repo.insert_session(session)

    e1 = Event(
        session_id=session.id,
        project_id=project.id,
        type="note",
        content="The API uses cursor-based pagination for list endpoints",
    )
    repo.insert_event(e1)

    e2 = Event(
        session_id=session.id,
        project_id=project.id,
        type="note",
        content="Redis caching layer was added to the auth module",
    )
    repo.insert_event(e2)

    todo = Entity(
        project_id=project.id,
        source_event_id=e1.id,
        type="todo",
        title="Add pagination tests",
        content="Write integration tests for cursor pagination",
        status="open",
        priority="high",
    )
    conn = memory_db.connect()
    try:
        row = todo.to_row()
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

    decision = Entity(
        project_id=project.id,
        source_event_id=e2.id,
        type="decision",
        title="Use Redis for caching",
        content="Chose Redis over Memcached for the auth caching layer",
    )
    conn = memory_db.connect()
    try:
        row = decision.to_row()
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


class TestRecencyFactor:
    def test_recent_gets_high_score(self) -> None:
        from datetime import datetime

        from callmem.compat import UTC

        now = datetime.now(UTC).isoformat()
        factor = _recency_factor(now, now)
        assert factor > 0.99

    def test_old_gets_low_score(self) -> None:
        factor = _recency_factor("2020-01-01T00:00:00+00:00")
        assert factor < 0.01


class TestFTS5Search:
    def test_search_returns_matching_events(
        self, memory_db: Database
    ) -> None:
        from callmem.models.config import Config

        project_id = _seed_data(memory_db)
        repo = __import__(
            "callmem.core.repository", fromlist=["Repository"]
        ).Repository(memory_db)
        engine = RetrievalEngine(repo, Config())
        results = engine.search(project_id, "pagination")
        assert len(results) > 0
        assert any("pagination" in r.content.lower() for r in results)

    def test_search_returns_empty_for_no_match(
        self, memory_db: Database
    ) -> None:
        from callmem.models.config import Config

        project_id = _seed_data(memory_db)
        repo = __import__(
            "callmem.core.repository", fromlist=["Repository"]
        ).Repository(memory_db)
        engine = RetrievalEngine(repo, Config())
        results = engine.search(project_id, "xyzzy_nonexistent")
        assert len(results) == 0


class TestStructuredSearch:
    def test_search_by_type_todo(self, memory_db: Database) -> None:
        from callmem.models.config import Config

        project_id = _seed_data(memory_db)
        repo = __import__(
            "callmem.core.repository", fromlist=["Repository"]
        ).Repository(memory_db)
        engine = RetrievalEngine(repo, Config())
        results = engine.search(project_id, "", types=["todo"])
        assert len(results) > 0
        assert all(r.type == "todo" for r in results)


class TestEntitySearch:
    def test_search_finds_entities_by_query(
        self, memory_db: Database
    ) -> None:
        from callmem.models.config import Config

        project_id = _seed_data(memory_db)
        repo = __import__(
            "callmem.core.repository", fromlist=["Repository"]
        ).Repository(memory_db)
        engine = RetrievalEngine(repo, Config())
        results = engine.search(project_id, "Redis")
        entity_results = [r for r in results if r.source_type == "entity"]
        assert len(entity_results) > 0


class TestDeduplication:
    def test_no_duplicate_ids(self, memory_db: Database) -> None:
        from callmem.models.config import Config

        project_id = _seed_data(memory_db)
        repo = __import__(
            "callmem.core.repository", fromlist=["Repository"]
        ).Repository(memory_db)
        engine = RetrievalEngine(repo, Config())
        results = engine.search(project_id, "pagination")
        ids = [r.id for r in results]
        assert len(ids) == len(set(ids))


class TestRecencyRanking:
    def test_recent_results_score_higher(
        self, memory_db: Database
    ) -> None:
        from callmem.models.config import Config

        project_id = _seed_data(memory_db)
        repo = __import__(
            "callmem.core.repository", fromlist=["Repository"]
        ).Repository(memory_db)
        engine = RetrievalEngine(repo, Config())
        results = engine.search(project_id, "")
        if len(results) >= 2:
            assert results[0].score >= results[-1].score


class TestEntitySearchNoRecencyWindow:
    """Entity search must find relevant entities regardless of age —
    the old fetch-20-most-recent-then-substring-filter defect made
    anything older than the freshest ~20 entities unreachable."""

    def test_old_entity_found_among_many_fresher_entities(
        self, memory_db: Database
    ) -> None:
        from datetime import datetime, timedelta

        from callmem.compat import UTC
        from callmem.core.repository import Repository
        from callmem.models.config import Config
        from callmem.models.entities import Entity
        from callmem.models.projects import Project

        repo = Repository(memory_db)
        project = Project(name="test-project")
        repo.create_project(project)

        old_ts = (datetime.now(UTC) - timedelta(days=30)).isoformat()
        repo.create_entity(Entity(
            project_id=project.id, type="fact",
            title="uses zorbatron protocol for auth",
            content="legacy detail",
            created_at=old_ts, updated_at=old_ts,
        ))

        for i in range(50):
            repo.create_entity(Entity(
                project_id=project.id, type="fact",
                title=f"unrelated fresh entity {i}",
                content="filler content",
            ))

        engine = RetrievalEngine(repo, Config())
        results = engine.search(
            project.id, "zorbatron", strategies=["entities"],
        )
        titles = [r.title or "" for r in results]
        assert any("zorbatron" in t.lower() for t in titles)

    def test_multiword_query_matches_regardless_of_word_order(
        self, memory_db: Database
    ) -> None:
        from callmem.core.repository import Repository
        from callmem.models.config import Config
        from callmem.models.entities import Entity
        from callmem.models.projects import Project

        repo = Repository(memory_db)
        project = Project(name="test-project")
        repo.create_project(project)

        entity = Entity(
            project_id=project.id, type="fact",
            title="cursor based pagination system",
            content="detail",
        )
        repo.create_entity(entity)

        engine = RetrievalEngine(repo, Config())
        results = engine.search(
            project.id, "pagination cursor", strategies=["entities"],
        )
        assert any(r.id == entity.id for r in results)


class TestHostileSearchQueries:
    """No query string may ever raise, whether run through the
    entity or event FTS strategies."""

    @pytest.mark.parametrize("query", [
        "cookie-backed",
        '"quoted phrase"',
        "AND)(",
        "a NOT b OR",
        "()()",
    ])
    def test_hostile_query_never_raises(
        self, memory_db: Database, query: str
    ) -> None:
        from callmem.models.config import Config

        project_id = _seed_data(memory_db)
        repo = __import__(
            "callmem.core.repository", fromlist=["Repository"]
        ).Repository(memory_db)
        engine = RetrievalEngine(repo, Config())
        results = engine.search(project_id, query)
        assert isinstance(results, list)


class TestGetRecent:
    def test_get_recent_returns_events(
        self, memory_db: Database
    ) -> None:
        from callmem.models.config import Config

        project_id = _seed_data(memory_db)
        repo = __import__(
            "callmem.core.repository", fromlist=["Repository"]
        ).Repository(memory_db)
        engine = RetrievalEngine(repo, Config())
        results = engine.get_recent(project_id, limit=10)
        assert len(results) > 0
        assert all(r.source_type == "event" for r in results)
