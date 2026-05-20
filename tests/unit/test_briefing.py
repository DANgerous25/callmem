"""Tests for the briefing generator."""

from __future__ import annotations

from typing import TYPE_CHECKING

from callmem.core.briefing import BriefingGenerator
from callmem.core.repository import Repository
from callmem.models.config import Config
from callmem.models.entities import Entity
from callmem.models.projects import Project
from callmem.models.sessions import Session

if TYPE_CHECKING:
    from callmem.core.database import Database
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

    from callmem.models.events import Event

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
        assert "Latest Session" in briefing.content
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
        # Budget must clear the protected tail (Suggested next + footer);
        # 300 is the smallest round budget that the populated fixture can
        # honor without dropping the tail.
        briefing = gen.generate(project_id, project_name="test", max_tokens=300)
        assert briefing.token_count <= 300

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
        assert "\U0001f7e2 feature" in briefing.content

    def test_briefing_includes_footer(
        self, memory_db: Database
    ) -> None:
        project_id = _seed_with_entities(memory_db)
        repo = Repository(memory_db)
        gen = BriefingGenerator(repo, Config())
        briefing = gen.generate(project_id, project_name="test")
        assert "Web UI:" in briefing.content

    def test_briefing_has_savings_pct(
        self, memory_db: Database
    ) -> None:
        project_id = _seed_with_entities(memory_db)
        repo = Repository(memory_db)
        gen = BriefingGenerator(repo, Config())
        briefing = gen.generate(project_id, project_name="test")
        assert isinstance(briefing.savings_pct, float)

    def test_briefing_includes_suggested_next(
        self, memory_db: Database
    ) -> None:
        project_id = _seed_with_entities(memory_db)
        repo = Repository(memory_db)
        gen = BriefingGenerator(repo, Config())
        briefing = gen.generate(project_id, project_name="test")
        # Fixture has one unresolved failure + one high-priority TODO, so
        # the section should appear and contain both.
        assert "Suggested next" in briefing.content
        assert "Database connection timeout" in briefing.content
        assert "Add auth middleware" in briefing.content
        # The "Suggested next" header should come AFTER the Action Items
        # block — it's a curated tail summary, not a duplicate up top.
        suggested_idx = briefing.content.index("Suggested next")
        action_idx = briefing.content.index("Action Items")
        assert suggested_idx > action_idx

    def test_suggested_next_omitted_when_no_qualifying_items(
        self, memory_db: Database
    ) -> None:
        # Seed a project with only a decision (no failures, no TODOs).
        repo = Repository(memory_db)
        project_id = _seed_project(memory_db)
        decision = Entity(
            project_id=project_id,
            type="decision",
            title="Use FastAPI",
            content="Chose FastAPI over Flask",
        )
        _insert_entity(memory_db, decision)
        gen = BriefingGenerator(repo, Config())
        briefing = gen.generate(project_id, project_name="test")
        assert "Suggested next" not in briefing.content

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
