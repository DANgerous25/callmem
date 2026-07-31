"""Tests for the briefing generator."""

from __future__ import annotations

from typing import TYPE_CHECKING

from callmem.core.briefing import BriefingGenerator
from callmem.core.queue import JobQueue
from callmem.core.repository import Repository
from callmem.models.config import Config
from callmem.models.entities import Entity
from callmem.models.events import Event
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
            "resolved_at, metadata, archived_at, cited_count, last_cited_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row["id"], row["project_id"], row["source_event_id"],
                row["type"], row["title"], row["content"],
                row["key_points"], row["synopsis"],
                row["status"], row["priority"], row["pinned"],
                row["created_at"], row["updated_at"],
                row["resolved_at"], row["metadata"], row["archived_at"],
                row["cited_count"], row["last_cited_at"],
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

    def _seed_failed_jobs_created_at(
        self, memory_db: Database, count: int, created_at: str,
    ) -> None:
        queue = JobQueue(memory_db)
        for _ in range(count):
            job_id = queue.enqueue("extract_entities", {}, max_attempts=1)
            queue.dequeue("extract_entities")
            queue.fail(job_id, "backend unreachable")
            conn = memory_db.connect()
            try:
                conn.execute(
                    "UPDATE jobs SET created_at = ? WHERE id = ?",
                    (created_at, job_id),
                )
                conn.commit()
            finally:
                conn.close()

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

    def test_old_failures_alone_do_not_trigger_banner(
        self, memory_db: Database,
    ) -> None:
        """Pins the live llm-mem/dj-mix-track-breaker state: dozens of
        all-time failed jobs, none created in the last 48h, extraction
        otherwise healthy. All-time debris must not cry wolf forever.
        """
        from datetime import datetime, timedelta

        from callmem.compat import UTC

        project_id = _seed_with_entities(memory_db)
        repo = Repository(memory_db)
        old_created_at = (
            datetime.now(UTC) - timedelta(hours=72)
        ).strftime("%Y-%m-%d %H:%M:%S")
        self._seed_failed_jobs_created_at(memory_db, 46, old_created_at)
        self._complete_job_at(
            memory_db, "extract_entities",
            datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S"),
        )

        gen = BriefingGenerator(repo, Config())
        briefing = gen.generate(project_id, project_name="test")
        assert "MEMORY PIPELINE UNHEALTHY" not in briefing.content
        assert briefing.pipeline_health["status"] == "healthy"
        assert briefing.pipeline_health["failed_jobs"] == 46
        assert briefing.pipeline_health["failed_jobs_recent"] == 0

    def test_recent_failures_over_threshold_trigger_banner_with_recent_count(
        self, memory_db: Database,
    ) -> None:
        project_id = _seed_with_entities(memory_db)
        repo = Repository(memory_db)
        self._seed_failed_jobs(memory_db, 25)

        gen = BriefingGenerator(repo, Config())
        briefing = gen.generate(project_id, project_name="test")
        assert "MEMORY PIPELINE UNHEALTHY" in briefing.content
        assert "25 failed jobs in 48h" in briefing.content
        assert briefing.pipeline_health["status"] == "unhealthy"
        assert briefing.pipeline_health["failed_jobs"] == 25
        assert briefing.pipeline_health["failed_jobs_recent"] == 25

    def test_mixed_old_and_recent_under_threshold_stays_healthy(
        self, memory_db: Database,
    ) -> None:
        """Old failures push the all-time total over threshold, but only a
        handful are recent — the recent count alone must decide health.
        """
        from datetime import datetime, timedelta

        from callmem.compat import UTC

        project_id = _seed_with_entities(memory_db)
        repo = Repository(memory_db)
        old_created_at = (
            datetime.now(UTC) - timedelta(hours=72)
        ).strftime("%Y-%m-%d %H:%M:%S")
        self._seed_failed_jobs_created_at(memory_db, 30, old_created_at)
        self._seed_failed_jobs(memory_db, 5)
        self._complete_job_at(
            memory_db, "extract_entities",
            datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S"),
        )

        gen = BriefingGenerator(repo, Config())
        briefing = gen.generate(project_id, project_name="test")
        assert "MEMORY PIPELINE UNHEALTHY" not in briefing.content
        assert briefing.pipeline_health["status"] == "healthy"
        assert briefing.pipeline_health["failed_jobs"] == 35
        assert briefing.pipeline_health["failed_jobs_recent"] == 5


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


def _days_ago(days: int) -> str:
    from datetime import datetime, timedelta

    from callmem.compat import UTC

    return (datetime.now(UTC) - timedelta(days=days)).isoformat()


class TestImportanceRankedSelection:
    """`_fetch_all_entities` ranks by an importance score, not pure
    recency: pinned boost + type weight + recency decay + citation boost
    (log-scaled cited_count, recency-decayed). See BriefingScoringConfig.
    """

    def _days_ago(self, days: int) -> str:
        return _days_ago(days)

    def test_old_cited_decision_beats_fresh_churn_change(
        self, memory_db: Database,
    ) -> None:
        repo = Repository(memory_db)
        project_id = _seed_project(memory_db)

        old_decision = Entity(
            project_id=project_id, type="decision",
            title="Old but frequently cited decision",
            content="Chose the event-sourced architecture",
            created_at=self._days_ago(60),
            cited_count=6,
            last_cited_at=self._days_ago(1),
        )
        _insert_entity(memory_db, old_decision)

        fresh_change = Entity(
            project_id=project_id, type="change",
            title="Renamed a variable",
            content="Minor rename, no functional impact",
            created_at=self._days_ago(0),
        )
        _insert_entity(memory_db, fresh_change)

        gen = BriefingGenerator(repo, Config())
        entities, _ = gen._fetch_all_entities(project_id, focus=None)
        ids_in_order = [e["id"] for e in entities]
        assert (
            ids_in_order.index(old_decision.id)
            < ids_in_order.index(fresh_change.id)
        )

    def test_uncited_old_entity_decays_below_fresh_uncited_entity(
        self, memory_db: Database,
    ) -> None:
        """3c: entities never cited, older than N days, and not
        pinned/todo-open must rank measurably lower than a fresh entity of
        the same type — the recency-decay term, isolated from every other
        score component."""
        repo = Repository(memory_db)
        project_id = _seed_project(memory_db)

        old_fact = Entity(
            project_id=project_id, type="fact",
            title="Old, never-cited, unpinned fact",
            content="Some fact nobody references anymore",
            created_at=self._days_ago(90),
        )
        _insert_entity(memory_db, old_fact)

        fresh_fact = Entity(
            project_id=project_id, type="fact",
            title="Fresh, never-cited, unpinned fact",
            content="Same type, just created today",
            created_at=self._days_ago(0),
        )
        _insert_entity(memory_db, fresh_fact)

        gen = BriefingGenerator(repo, Config())
        entities, _ = gen._fetch_all_entities(project_id, focus=None)
        ids_in_order = [e["id"] for e in entities]
        assert (
            ids_in_order.index(fresh_fact.id)
            < ids_in_order.index(old_fact.id)
        )

    def test_type_weight_tier_ordering(self, memory_db: Database) -> None:
        repo = Repository(memory_db)
        project_id = _seed_project(memory_db)
        now = self._days_ago(0)

        tiers = [
            ("decision", "Tier 1 decision"),
            ("bugfix", "Tier 2 bugfix"),
            ("todo", "Tier 3 todo"),
            ("change", "Tier 4 change"),
        ]
        created = []
        for etype, title in tiers:
            e = Entity(
                project_id=project_id, type=etype, title=title,
                content="body", created_at=now,
            )
            _insert_entity(memory_db, e)
            created.append(e)

        gen = BriefingGenerator(repo, Config())
        entities, _ = gen._fetch_all_entities(project_id, focus=None)
        ids_in_order = [e["id"] for e in entities]
        ranks = [ids_in_order.index(e.id) for e in created]
        assert ranks == sorted(ranks)

    def test_fresh_session_entities_always_present_despite_low_score(
        self, memory_db: Database,
    ) -> None:
        """Entities from the most recent session are a floor: the
        max_entities budget trim must never drop them, even when their
        score would otherwise put them last."""
        repo = Repository(memory_db)
        project_id = _seed_project(memory_db)

        old_session = Session(
            project_id=project_id, started_at=self._days_ago(30),
        )
        repo.insert_session(old_session)
        new_session = Session(
            project_id=project_id, started_at=self._days_ago(0),
        )
        repo.insert_session(new_session)

        old_event = Event(
            session_id=old_session.id, project_id=project_id,
            type="note", content="old note",
        )
        repo.insert_event(old_event)
        new_event = Event(
            session_id=new_session.id, project_id=project_id,
            type="note", content="new note",
        )
        repo.insert_event(new_event)

        # High-scoring pinned decisions from the OLD session fill up a
        # deliberately tiny budget ahead of the low-value fresh entity.
        filler_ids = []
        for i in range(3):
            filler = Entity(
                project_id=project_id, type="decision",
                title=f"Pinned filler decision {i}",
                content="Important pinned decision",
                pinned=True,
                source_event_id=old_event.id,
            )
            _insert_entity(memory_db, filler)
            filler_ids.append(filler.id)

        # Low-value entity (uncited churn, unpinned) but from the MOST
        # RECENT session — must survive the trim regardless of its score.
        fresh_low_value = Entity(
            project_id=project_id, type="change",
            title="Trivial fresh-session churn",
            content="A minor change with no other signal",
            source_event_id=new_event.id,
        )
        _insert_entity(memory_db, fresh_low_value)

        config = Config()
        config.briefing.scoring.max_entities = 3
        gen = BriefingGenerator(repo, config)
        entities, _ = gen._fetch_all_entities(project_id, focus=None)
        ids = {e["id"] for e in entities}

        assert fresh_low_value.id in ids
        for fid in filler_ids:
            assert fid in ids
        assert len(entities) == 4


class TestOpenItemsFloor:
    """Open todo/failure entities are a second always-include floor: an
    old, unpinned, uncited open TODO must not silently vanish from the
    briefing just because it loses on score to a flood of fresher/noisier
    entities. See BriefingGenerator._open_items_floor_ids."""

    def test_old_open_todo_survives_against_fresher_competition(
        self, memory_db: Database,
    ) -> None:
        repo = Repository(memory_db)
        project_id = _seed_project(memory_db)

        old_todo = Entity(
            project_id=project_id, type="todo", status="open",
            title="Old open todo nobody prioritized",
            content="Still needs doing",
            created_at=_days_ago(100),
        )
        _insert_entity(memory_db, old_todo)

        # 200 fresh, pinned, high-scoring decisions crowd out everything
        # that competes purely on score — max_entities defaults to 100,
        # so without the open-items floor the old todo would be dropped.
        for i in range(200):
            filler = Entity(
                project_id=project_id, type="decision",
                title=f"Fresh pinned decision {i}",
                content="High-value pinned decision",
                pinned=True,
            )
            _insert_entity(memory_db, filler)

        gen = BriefingGenerator(repo, Config())
        entities, _ = gen._fetch_all_entities(project_id, focus=None)
        ids = {e["id"] for e in entities}

        assert old_todo.id in ids

    def test_open_items_floor_cap_drops_lowest_priority_beyond_cap(
        self, memory_db: Database,
    ) -> None:
        repo = Repository(memory_db)
        project_id = _seed_project(memory_db)

        # 25 old, unpinned, uncited open todos — more than the default
        # floor cap (20): 5 high-priority, 20 unprioritized.
        high_ids = set()
        unprioritized_ids = set()
        for i in range(25):
            priority = "high" if i < 5 else None
            todo = Entity(
                project_id=project_id, type="todo", status="open",
                title=f"Old open todo {i}",
                content="Needs doing",
                created_at=_days_ago(100),
                priority=priority,
            )
            _insert_entity(memory_db, todo)
            if priority == "high":
                high_ids.add(todo.id)
            else:
                unprioritized_ids.add(todo.id)

        config = Config()
        # Force every entity through the floor only, so the returned set
        # is exactly what the open-items floor (cap=20 default) allows.
        config.briefing.scoring.max_entities = 0
        gen = BriefingGenerator(repo, config)
        entities, _ = gen._fetch_all_entities(project_id, focus=None)
        ids = {e["id"] for e in entities}

        assert len(ids) == 20
        assert high_ids <= ids
        assert len(unprioritized_ids & ids) == 15

    def test_open_items_floor_cap_is_configurable(
        self, memory_db: Database,
    ) -> None:
        repo = Repository(memory_db)
        project_id = _seed_project(memory_db)

        for i in range(10):
            todo = Entity(
                project_id=project_id, type="todo", status="open",
                title=f"Old open todo {i}", content="Needs doing",
                created_at=_days_ago(100),
            )
            _insert_entity(memory_db, todo)

        config = Config()
        config.briefing.scoring.max_entities = 0
        config.briefing.scoring.open_items_floor_cap = 3
        gen = BriefingGenerator(repo, config)
        entities, _ = gen._fetch_all_entities(project_id, focus=None)

        assert len(entities) == 3

    def test_resolved_todo_does_not_occupy_floor_slot(
        self, memory_db: Database,
    ) -> None:
        repo = Repository(memory_db)
        project_id = _seed_project(memory_db)

        resolved = Entity(
            project_id=project_id, type="todo", status="resolved",
            title="Already resolved", content="Done",
            created_at=_days_ago(100),
        )
        _insert_entity(memory_db, resolved)

        config = Config()
        config.briefing.scoring.max_entities = 0
        gen = BriefingGenerator(repo, config)
        entities, _ = gen._fetch_all_entities(project_id, focus=None)

        assert resolved.id not in {e["id"] for e in entities}

    def test_action_items_excludes_done_and_cancelled_todos(
        self, memory_db: Database,
    ) -> None:
        """mem_resolve's whole point is that a resolved TODO disappears
        from Action Items by status, not staleness. Only 'resolved' was
        previously excluded from the render filter -- a fresh 'done' or
        'cancelled' todo scores high enough (via recency) to land in
        Action Items anyway unless the filter also excludes them."""
        repo = Repository(memory_db)
        project_id = _seed_project(memory_db)

        done_todo = Entity(
            project_id=project_id, type="todo", status="done",
            title="Finished todo should not resurface",
            content="x", priority="high",
        )
        _insert_entity(memory_db, done_todo)
        cancelled_todo = Entity(
            project_id=project_id, type="todo", status="cancelled",
            title="Cancelled todo should not resurface",
            content="x", priority="high",
        )
        _insert_entity(memory_db, cancelled_todo)
        open_todo = Entity(
            project_id=project_id, type="todo", status="open",
            title="Still open todo should show up",
            content="x", priority="high",
        )
        _insert_entity(memory_db, open_todo)

        gen = BriefingGenerator(repo, Config())
        briefing = gen.generate(project_id, project_name="test")

        # Action Items / Suggested next lines are rendered with a
        # two-space-then-'#' prefix; the generic date-grouped listing
        # (which every non-stale entity passes through regardless of
        # status) uses a four-space prefix, so this precisely scopes the
        # assertion to the action-item-style lines.
        action_lines = [
            line for line in briefing.content.splitlines()
            if line.startswith("  #")
        ]
        action_text = "\n".join(action_lines)
        assert "Finished todo should not resurface" not in action_text
        assert "Cancelled todo should not resurface" not in action_text
        assert "Still open todo should show up" in action_text


class TestArchivedEntitiesExcluded:
    """Archived entities (archived_at set — e.g. NOOP-archived duplicates
    from consolidation, or originals archived by re-extraction) must never
    surface in the briefing, even when every other score signal is
    maximal. Covers both _fetch_all_entities (the ranked/scored path) and
    _fetch_entities_for_session (the most-recent-session floor)."""

    def test_archived_entity_excluded_from_ranked_selection(
        self, memory_db: Database,
    ) -> None:
        repo = Repository(memory_db)
        project_id = _seed_project(memory_db)

        archived = Entity(
            project_id=project_id, type="decision",
            title="Archived duplicate decision",
            content="Should never surface",
            pinned=True,
            created_at=_days_ago(0),
            cited_count=50,
            last_cited_at=_days_ago(0),
            archived_at=_days_ago(0),
        )
        _insert_entity(memory_db, archived)

        survivor = Entity(
            project_id=project_id, type="fact",
            title="Ordinary survivor fact",
            content="A perfectly normal entity",
            created_at=_days_ago(0),
        )
        _insert_entity(memory_db, survivor)

        gen = BriefingGenerator(repo, Config())
        entities, _ = gen._fetch_all_entities(project_id, focus=None)
        ids = {e["id"] for e in entities}

        assert archived.id not in ids
        assert survivor.id in ids

    def test_archived_entity_excluded_from_session_floor(
        self, memory_db: Database,
    ) -> None:
        repo = Repository(memory_db)
        project_id = _seed_project(memory_db)

        session = Session(project_id=project_id, started_at=_days_ago(0))
        repo.insert_session(session)
        event = Event(
            session_id=session.id, project_id=project_id,
            type="note", content="latest session note",
        )
        repo.insert_event(event)

        archived = Entity(
            project_id=project_id, type="decision",
            title="Archived entity from latest session",
            content="Superseded original, archived by re-extraction",
            pinned=True,
            source_event_id=event.id,
            created_at=_days_ago(0),
            cited_count=50,
            last_cited_at=_days_ago(0),
            archived_at=_days_ago(0),
        )
        _insert_entity(memory_db, archived)

        gen = BriefingGenerator(repo, Config())

        session_entities = gen._fetch_entities_for_session(session.id)
        assert archived.id not in {e["id"] for e in session_entities}

        entities, _ = gen._fetch_all_entities(project_id, focus=None)
        assert archived.id not in {e["id"] for e in entities}


def _seed_project_with_root(memory_db: Database, root_path) -> str:
    repo = Repository(memory_db)
    project = Project(name="test-project", root_path=str(root_path))
    repo.create_project(project)
    return project.id


def _insert_entity_file(
    memory_db: Database,
    entity_id: str,
    file_path: str,
    line_number: int | None = None,
) -> None:
    conn = memory_db.connect()
    try:
        conn.execute(
            "INSERT INTO entity_files "
            "(entity_id, file_path, relation, line_number) "
            "VALUES (?, ?, 'related', ?)",
            (entity_id, file_path, line_number),
        )
        conn.commit()
    finally:
        conn.close()


class TestStaleAnchorAnnotation:
    """5b: briefing render validates code anchors for entities being
    surfaced and annotates missing files inline (annotation only —
    phase-1 scope never auto-stales the entity)."""

    def test_missing_file_gets_annotated_in_briefing(
        self, memory_db: Database, tmp_path,
    ) -> None:
        project_id = _seed_project_with_root(memory_db, tmp_path)
        repo = Repository(memory_db)
        todo = Entity(
            project_id=project_id, type="todo", status="open",
            title="Fix the widget", content="See widget.py",
        )
        _insert_entity(memory_db, todo)
        _insert_entity_file(memory_db, todo.id, "widget.py")

        gen = BriefingGenerator(repo, Config())
        briefing = gen.generate(project_id, project_name="test")

        assert "⚠ file gone: widget.py" in briefing.content

    def test_existing_file_is_not_annotated(
        self, memory_db: Database, tmp_path,
    ) -> None:
        project_id = _seed_project_with_root(memory_db, tmp_path)
        repo = Repository(memory_db)
        (tmp_path / "widget.py").write_text("x = 1\n")
        todo = Entity(
            project_id=project_id, type="todo", status="open",
            title="Fix the widget", content="See widget.py",
        )
        _insert_entity(memory_db, todo)
        _insert_entity_file(memory_db, todo.id, "widget.py")

        gen = BriefingGenerator(repo, Config())
        briefing = gen.generate(project_id, project_name="test")

        assert "file gone" not in briefing.content

    def test_missing_file_does_not_mark_entity_stale(
        self, memory_db: Database, tmp_path,
    ) -> None:
        project_id = _seed_project_with_root(memory_db, tmp_path)
        repo = Repository(memory_db)
        todo = Entity(
            project_id=project_id, type="todo", status="open",
            title="Fix the widget", content="See widget.py",
        )
        _insert_entity(memory_db, todo)
        _insert_entity_file(memory_db, todo.id, "widget.py")

        gen = BriefingGenerator(repo, Config())
        gen.generate(project_id, project_name="test")

        stored = repo.get_entity(todo.id)
        assert stored is not None
        assert not stored.get("stale")

    def test_no_project_root_skips_validation_without_crashing(
        self, memory_db: Database,
    ) -> None:
        project_id = _seed_project(memory_db)  # no root_path set
        repo = Repository(memory_db)
        todo = Entity(
            project_id=project_id, type="todo", status="open",
            title="Fix the widget", content="See widget.py",
        )
        _insert_entity(memory_db, todo)
        _insert_entity_file(memory_db, todo.id, "widget.py")

        gen = BriefingGenerator(repo, Config())
        briefing = gen.generate(project_id, project_name="test")

        assert "file gone" not in briefing.content

    def test_path_outside_root_is_never_statted(
        self, memory_db: Database, tmp_path, monkeypatch,
    ) -> None:
        """Security: an anchor that resolves outside the project root
        must never reach the filesystem, even indirectly via a full
        briefing render."""
        from pathlib import Path

        project_id = _seed_project_with_root(memory_db, tmp_path)
        repo = Repository(memory_db)
        todo = Entity(
            project_id=project_id, type="todo", status="open",
            title="Suspicious anchor", content="unrelated",
        )
        _insert_entity(memory_db, todo)
        _insert_entity_file(memory_db, todo.id, "../../etc/passwd")

        calls: list[str] = []
        real_exists = Path.exists

        def counting_exists(self: Path) -> bool:
            calls.append(str(self))
            return real_exists(self)

        monkeypatch.setattr(Path, "exists", counting_exists)

        gen = BriefingGenerator(repo, Config())
        briefing = gen.generate(project_id, project_name="test")

        assert not any("passwd" in c for c in calls)
        assert "file gone" not in briefing.content

    def test_anchor_validation_cached_across_two_pass_render(
        self, memory_db: Database, tmp_path, monkeypatch,
    ) -> None:
        """generate() renders the body twice (a provisional pass to
        measure the token budget, then the final pass) — anchor
        validation must be computed once and reused, not stat the same
        file twice."""
        from pathlib import Path

        project_id = _seed_project_with_root(memory_db, tmp_path)
        repo = Repository(memory_db)
        todo = Entity(
            project_id=project_id, type="todo", status="open",
            title="Fix the widget", content="See widget.py",
        )
        _insert_entity(memory_db, todo)
        _insert_entity_file(memory_db, todo.id, "widget.py")

        calls: list[str] = []
        real_exists = Path.exists

        def counting_exists(self: Path) -> bool:
            calls.append(str(self))
            return real_exists(self)

        monkeypatch.setattr(Path, "exists", counting_exists)

        gen = BriefingGenerator(repo, Config())
        gen.generate(project_id, project_name="test")

        widget_calls = [c for c in calls if c.endswith("widget.py")]
        assert len(widget_calls) == 1

    def test_null_root_path_self_heals_and_validates_real_file(
        self, tmp_path,
    ) -> None:
        """A legacy project row with NULL root_path (predating root_path
        being populated at creation) must self-heal from the database's
        own path the first time an anchor is read, and validation must
        actually run afterwards rather than staying permanently dead."""
        from callmem.core.database import Database

        db = Database(tmp_path / ".callmem" / "memory.db")
        db.initialize()
        repo = Repository(db)
        project = Project(name="test-project")  # root_path NULL
        repo.create_project(project)

        (tmp_path / "widget.py").write_text("x = 1\n")
        todo = Entity(
            project_id=project.id, type="todo", status="open",
            title="Fix the widget", content="See widget.py",
        )
        _insert_entity(db, todo)
        _insert_entity_file(db, todo.id, "widget.py")

        gen = BriefingGenerator(repo, Config())
        briefing = gen.generate(project.id, project_name="test")

        assert "file gone" not in briefing.content
        healed = repo.get_project(project.id)
        assert healed is not None
        assert healed.root_path == str(tmp_path)
