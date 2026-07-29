"""Tests for the briefing generator."""

from __future__ import annotations

from typing import TYPE_CHECKING

from callmem.core.briefing import BriefingGenerator
from callmem.core.queue import JobQueue
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

    from datetime import datetime

    from callmem.compat import UTC

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

    def test_briefing_extraction_warning_when_events_but_no_entities(
        self, memory_db: Database,
    ) -> None:
        project_id = _seed_project(memory_db)
        repo = Repository(memory_db)

        session = Session(project_id=project_id)
        repo.insert_session(session)

        from callmem.models.events import Event
        for i in range(5):
            event = Event(
                session_id=session.id,
                project_id=project_id,
                type="note",
                content=f"test event {i}",
            )
            repo.insert_event(event)

        gen = BriefingGenerator(repo, Config())
        briefing = gen.generate(project_id, project_name="test")
        assert "0 entities extracted" in briefing.content
        assert "callmem doctor" in briefing.content

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


class TestPipelineHealth:
    """Pipeline health banner: unhealthy on too many failed jobs, or when
    events keep arriving but extraction has stalled.
    """

    def _seed_failed_jobs(self, memory_db: Database, count: int) -> None:
        queue = JobQueue(memory_db)
        for _ in range(count):
            job_id = queue.enqueue("extract_entities", {}, max_attempts=1)
            queue.dequeue("extract_entities")
            queue.fail(job_id, "backend unreachable")

    def _complete_job_at(
        self, memory_db: Database, job_type: str, completed_at: str,
    ) -> None:
        queue = JobQueue(memory_db)
        job_id = queue.enqueue(job_type, {})
        queue.dequeue(job_type)
        queue.complete(job_id)
        conn = memory_db.connect()
        try:
            conn.execute(
                "UPDATE jobs SET completed_at = ? WHERE id = ?",
                (completed_at, job_id),
            )
            conn.commit()
        finally:
            conn.close()

    def test_many_failed_jobs_trigger_banner(
        self, memory_db: Database,
    ) -> None:
        project_id = _seed_with_entities(memory_db)
        self._seed_failed_jobs(memory_db, 21)
        repo = Repository(memory_db)
        gen = BriefingGenerator(repo, Config())
        briefing = gen.generate(project_id, project_name="test")
        assert "MEMORY PIPELINE UNHEALTHY" in briefing.content
        assert "21 failed jobs" in briefing.content
        assert "callmem requeue-failed" in briefing.content
        assert briefing.pipeline_health["status"] == "unhealthy"
        assert briefing.pipeline_health["failed_jobs"] == 21
        # Banner must appear before entity sections.
        banner_idx = briefing.content.index("MEMORY PIPELINE UNHEALTHY")
        action_idx = briefing.content.index("Add auth middleware")
        assert banner_idx < action_idx

    def test_stale_extraction_with_fresh_events_triggers_banner(
        self, memory_db: Database,
    ) -> None:
        from datetime import datetime, timedelta

        from callmem.compat import UTC

        # _seed_with_entities already inserts one event stamped "now".
        project_id = _seed_with_entities(memory_db)
        repo = Repository(memory_db)

        stale_completed_at = (
            datetime.now(UTC) - timedelta(days=10)
        ).strftime("%Y-%m-%d %H:%M:%S")
        self._complete_job_at(memory_db, "extract_entities", stale_completed_at)

        gen = BriefingGenerator(repo, Config())
        briefing = gen.generate(project_id, project_name="test")
        assert "MEMORY PIPELINE UNHEALTHY" in briefing.content
        assert briefing.pipeline_health["status"] == "unhealthy"
        assert briefing.pipeline_health["days_since_last_extraction"] == 10

    def test_never_completed_extraction_with_pending_jobs_triggers_banner(
        self, memory_db: Database,
    ) -> None:
        # extract_entities jobs exist (one failed, one still pending) but
        # NONE has ever completed — extraction has never worked, not just
        # regressed. Well under the failed-count threshold on its own, so
        # this must be caught by the never-completed condition alone.
        project_id = _seed_with_entities(memory_db)
        repo = Repository(memory_db)

        queue = JobQueue(memory_db)
        failed_id = queue.enqueue("extract_entities", {}, max_attempts=1)
        queue.dequeue("extract_entities")
        queue.fail(failed_id, "backend unreachable")
        queue.enqueue("extract_entities", {})  # stays pending

        gen = BriefingGenerator(repo, Config())
        briefing = gen.generate(project_id, project_name="test")
        assert "MEMORY PIPELINE UNHEALTHY" in briefing.content
        assert "never" in briefing.content
        assert briefing.pipeline_health["status"] == "unhealthy"
        assert briefing.pipeline_health["failed_jobs"] == 1
        assert briefing.pipeline_health["days_since_last_extraction"] is None

    def test_no_job_queue_history_stays_healthy(
        self, memory_db: Database,
    ) -> None:
        # Legacy/synchronous-extraction project (or an empty test fixture):
        # entities exist but no extract_entities job row was ever created.
        # Fresh events alone must not trigger the never-completed condition.
        project_id = _seed_with_entities(memory_db)
        repo = Repository(memory_db)

        gen = BriefingGenerator(repo, Config())
        briefing = gen.generate(project_id, project_name="test")
        assert "MEMORY PIPELINE UNHEALTHY" not in briefing.content
        assert briefing.pipeline_health["status"] == "healthy"
        assert briefing.pipeline_health["days_since_last_extraction"] is None

    def test_healthy_db_has_no_banner(self, memory_db: Database) -> None:
        from datetime import datetime

        from callmem.compat import UTC

        project_id = _seed_with_entities(memory_db)
        repo = Repository(memory_db)
        self._complete_job_at(
            memory_db, "extract_entities",
            datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S"),
        )

        gen = BriefingGenerator(repo, Config())
        briefing = gen.generate(project_id, project_name="test")
        assert "MEMORY PIPELINE UNHEALTHY" not in briefing.content
        assert briefing.pipeline_health["status"] == "healthy"
        assert briefing.pipeline_health["failed_jobs"] == 0


class TestParseDbTimestamp:
    """`_parse_db_timestamp` must accept Z-suffixed timestamps.

    Python 3.10's ``datetime.fromisoformat`` rejects the ``Z`` UTC suffix
    (only 3.11+ accepts it). Production runs 3.10, and Claude Code
    transcript passthrough fills the DB with Z-suffixed event timestamps
    (e.g. ``2026-07-29T11:53:39.725Z``), so this must be normalized before
    parsing regardless of interpreter version.
    """

    def test_z_suffixed_timestamp_parses(self) -> None:
        from datetime import datetime

        from callmem.compat import UTC
        from callmem.core.briefing import _parse_db_timestamp

        parsed = _parse_db_timestamp("2026-07-29T11:53:39.725Z")
        assert parsed == datetime(2026, 7, 29, 11, 53, 39, 725000, tzinfo=UTC)

    def test_z_suffixed_timestamp_without_fraction_parses(self) -> None:
        from datetime import datetime

        from callmem.compat import UTC
        from callmem.core.briefing import _parse_db_timestamp

        parsed = _parse_db_timestamp("2026-07-29T11:53:39Z")
        assert parsed == datetime(2026, 7, 29, 11, 53, 39, tzinfo=UTC)
