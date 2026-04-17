"""Tests for the briefing generator."""

from __future__ import annotations

from typing import TYPE_CHECKING

from llm_mem.core.briefing import BriefingGenerator
from llm_mem.core.repository import Repository
from llm_mem.models.config import Config
from llm_mem.models.entities import Entity
from llm_mem.models.projects import Project
from llm_mem.models.sessions import Session

if TYPE_CHECKING:
    from llm_mem.core.database import Database
    pass


def _seed_project(memory_db: Database) -> str:
    repo = Repository(memory_db)
    project = Project(name="test-project")
    repo.create_project(project)
    return project.id


def _seed_with_entities(memory_db: Database) -> str:
    repo = Repository(memory_db)
    project_id = _seed_project(memory_db)

    session = Session(project_id=project_id)
    repo.insert_session(session)

    todo = Entity(
        project_id=project_id,
        type="todo",
        title="Add auth middleware",
        content="Implement JWT auth middleware for all API routes",
        status="open",
        priority="high",
    )
    _insert_entity(memory_db, todo)

    decision = Entity(
        project_id=project_id,
        type="decision",
        title="Use FastAPI",
        content="Chose FastAPI over Flask for the REST API",
    )
    _insert_entity(memory_db, decision)

    failure = Entity(
        project_id=project_id,
        type="failure",
        title="Database connection timeout",
        content="Intermittent timeouts connecting to Postgres",
        status="unresolved",
    )
    _insert_entity(memory_db, failure)

    fact = Entity(
        project_id=project_id,
        type="fact",
        title="API uses cursor pagination",
        content="All list endpoints use cursor-based pagination",
        pinned=True,
    )
    _insert_entity(memory_db, fact)

    from datetime import UTC, datetime

    from llm_mem.models.events import Event

    event = Event(
        session_id=session.id,
        project_id=project_id,
        type="note",
        content="some note",
    )
    repo.insert_event(event)

    session.status = "ended"
    session.ended_at = datetime.now(UTC).isoformat()
    session.summary = "Implemented auth and fixed database issues"
    session.event_count = 5
    repo.update_session(session)

    return project_id


def _insert_entity(memory_db: Database, entity: Entity) -> None:
    conn = memory_db.connect()
    try:
        row = entity.to_row()
        conn.execute(
            "INSERT INTO entities "
            "(id, project_id, source_event_id, type, title, content, "
            "key_points, synopsis, "
            "status, priority, pinned, created_at, updated_at, "
            "resolved_at, metadata, archived_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row["id"], row["project_id"], row["source_event_id"],
                row["type"], row["title"], row["content"],
                row["key_points"], row["synopsis"],
                row["status"], row["priority"], row["pinned"],
                row["created_at"], row["updated_at"],
                row["resolved_at"], row["metadata"], row["archived_at"],
            ),
        )
        conn.commit()
    finally:
        conn.close()


class TestBriefingGeneration:
    def test_briefing_includes_entities(self, memory_db: Database) -> None:
        project_id = _seed_with_entities(memory_db)
        repo = Repository(memory_db)
        gen = BriefingGenerator(repo, Config())
        briefing = gen.generate(project_id, project_name="test")
        assert "Add auth middleware" in briefing.content
        assert "Use FastAPI" in briefing.content

    def test_briefing_includes_session_summary(
        self, memory_db: Database
    ) -> None:
        project_id = _seed_with_entities(memory_db)
        repo = Repository(memory_db)
        gen = BriefingGenerator(repo, Config())
        briefing = gen.generate(project_id, project_name="test")
        assert "Latest Session Summary" in briefing.content
        assert "Implemented auth" in briefing.content

    def test_briefing_new_project(self, memory_db: Database) -> None:
        project_id = _seed_project(memory_db)
        repo = Repository(memory_db)
        gen = BriefingGenerator(repo, Config())
        briefing = gen.generate(project_id, project_name="newproj")
        assert briefing.token_count > 0
        assert "new project" in briefing.content.lower() or "no prior" in briefing.content.lower()

    def test_briefing_respects_token_budget(
        self, memory_db: Database
    ) -> None:
        project_id = _seed_with_entities(memory_db)
        repo = Repository(memory_db)
        gen = BriefingGenerator(repo, Config())
        briefing = gen.generate(project_id, project_name="test", max_tokens=50)
        assert briefing.token_count <= 50

    def test_briefing_components_populated(
        self, memory_db: Database
    ) -> None:
        project_id = _seed_with_entities(memory_db)
        repo = Repository(memory_db)
        gen = BriefingGenerator(repo, Config())
        briefing = gen.generate(project_id, project_name="test")
        assert len(briefing.components) > 0

    def test_briefing_focus_parameter(
        self, memory_db: Database
    ) -> None:
        project_id = _seed_with_entities(memory_db)
        repo = Repository(memory_db)
        gen = BriefingGenerator(repo, Config())
        briefing = gen.generate(
            project_id, project_name="test", focus="auth"
        )
        assert briefing.token_count > 0

    def test_briefing_has_generated_at(self, memory_db: Database) -> None:
        project_id = _seed_with_entities(memory_db)
        repo = Repository(memory_db)
        gen = BriefingGenerator(repo, Config())
        briefing = gen.generate(project_id, project_name="test")
        assert briefing.generated_at is not None

    def test_briefing_includes_context_economics(
        self, memory_db: Database
    ) -> None:
        project_id = _seed_with_entities(memory_db)
        repo = Repository(memory_db)
        gen = BriefingGenerator(repo, Config())
        briefing = gen.generate(project_id, project_name="test")
        assert briefing.observations_loaded > 0
        assert briefing.read_tokens > 0
        assert briefing.work_investment >= 0
        assert "Context Economics" in briefing.content

    def test_briefing_includes_legend(
        self, memory_db: Database
    ) -> None:
        project_id = _seed_with_entities(memory_db)
        repo = Repository(memory_db)
        gen = BriefingGenerator(repo, Config())
        briefing = gen.generate(project_id, project_name="test")
        assert "Legend:" in briefing.content

    def test_briefing_includes_footer(
        self, memory_db: Database
    ) -> None:
        project_id = _seed_with_entities(memory_db)
        repo = Repository(memory_db)
        gen = BriefingGenerator(repo, Config())
        briefing = gen.generate(project_id, project_name="test")
        assert "View observations live" in briefing.content

    def test_briefing_has_savings_pct(
        self, memory_db: Database
    ) -> None:
        project_id = _seed_with_entities(memory_db)
        repo = Repository(memory_db)
        gen = BriefingGenerator(repo, Config())
        briefing = gen.generate(project_id, project_name="test")
        assert isinstance(briefing.savings_pct, float)

    def test_write_session_summary(
        self, memory_db: Database, tmp_path
    ) -> None:
        project_id = _seed_with_entities(memory_db)
        repo = Repository(memory_db)
        gen = BriefingGenerator(repo, Config())
        briefing = gen.write_session_summary(
            project_id, "test", tmp_path
        )
        summary_path = tmp_path / "SESSION_SUMMARY.md"
        assert summary_path.exists()
        content = summary_path.read_text()
        assert len(content) > 0
        assert isinstance(briefing.observations_loaded, int)
