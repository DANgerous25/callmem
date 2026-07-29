"""Tests for the SQLite-backed job queue."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from callmem.compat import UTC
from callmem.core.queue import JobQueue

if TYPE_CHECKING:
    from callmem.core.database import Database


class TestEnqueue:
    def test_enqueue_returns_job_id(self, memory_db: Database) -> None:
        queue = JobQueue(memory_db)
        job_id = queue.enqueue("extract_entities", {"event_ids": ["abc"]})
        assert job_id is not None
        assert len(job_id) == 26  # ULID length

    def test_enqueue_stores_payload(self, memory_db: Database) -> None:
        queue = JobQueue(memory_db)
        job_id = queue.enqueue(
            "extract_entities", {"event_ids": ["a", "b"], "session_id": "s1"}
        )
        job = queue.get_job(job_id)
        assert job is not None
        assert job.payload["event_ids"] == ["a", "b"]
        assert job.payload["session_id"] == "s1"

    def test_enqueue_default_pending(self, memory_db: Database) -> None:
        queue = JobQueue(memory_db)
        job_id = queue.enqueue("extract_entities", {})
        job = queue.get_job(job_id)
        assert job is not None
        assert job.status == "pending"
        assert job.attempts == 0


class TestDequeue:
    def test_dequeue_returns_pending_job(self, memory_db: Database) -> None:
        queue = JobQueue(memory_db)
        queue.enqueue("extract_entities", {"event_ids": ["abc"]})
        job = queue.dequeue("extract_entities")
        assert job is not None
        assert job.status == "running"
        assert job.attempts == 1

    def test_dequeue_returns_none_when_empty(
        self, memory_db: Database
    ) -> None:
        queue = JobQueue(memory_db)
        job = queue.dequeue("extract_entities")
        assert job is None

    def test_dequeue_filters_by_type(self, memory_db: Database) -> None:
        queue = JobQueue(memory_db)
        queue.enqueue("extract_entities", {"x": 1})
        queue.enqueue("generate_summary", {"x": 2})
        job = queue.dequeue("generate_summary")
        assert job is not None
        assert job.type == "generate_summary"

    def test_dequeue_fifo_order(self, memory_db: Database) -> None:
        queue = JobQueue(memory_db)
        first = queue.enqueue("extract_entities", {"order": 1})
        queue.enqueue("extract_entities", {"order": 2})
        job = queue.dequeue("extract_entities")
        assert job is not None
        assert job.id == first

    def test_dequeue_all_types(self, memory_db: Database) -> None:
        queue = JobQueue(memory_db)
        queue.enqueue("extract_entities", {})
        queue.enqueue("generate_summary", {})
        job = queue.dequeue()
        assert job is not None
        job2 = queue.dequeue()
        assert job2 is not None


class TestComplete:
    def test_complete_marks_done(self, memory_db: Database) -> None:
        queue = JobQueue(memory_db)
        job_id = queue.enqueue("extract_entities", {})
        queue.dequeue("extract_entities")
        queue.complete(job_id)
        job = queue.get_job(job_id)
        assert job is not None
        assert job.status == "completed"
        assert job.completed_at is not None


class TestFail:
    def test_fail_retries_under_max(self, memory_db: Database) -> None:
        queue = JobQueue(memory_db)
        job_id = queue.enqueue("extract_entities", {}, max_attempts=3)
        queue.dequeue("extract_entities")
        queue.fail(job_id, "timeout")

        job = queue.get_job(job_id)
        assert job is not None
        assert job.status == "pending"
        assert job.error == "timeout"

    def test_fail_gives_up_after_max_attempts(
        self, memory_db: Database
    ) -> None:
        queue = JobQueue(memory_db)
        job_id = queue.enqueue("extract_entities", {}, max_attempts=2)

        queue.dequeue("extract_entities")
        queue.fail(job_id, "error 1")

        # The backoff from the first failure pushes next_attempt_at into
        # the future — fast-forward past it so the test doesn't need to
        # actually sleep out the 60s delay.
        conn = memory_db.connect()
        try:
            conn.execute(
                "UPDATE jobs SET next_attempt_at = datetime('now', '-1 seconds') "
                "WHERE id = ?",
                (job_id,),
            )
            conn.commit()
        finally:
            conn.close()

        queue.dequeue("extract_entities")
        queue.fail(job_id, "error 2")

        job = queue.get_job(job_id)
        assert job is not None
        assert job.status == "failed"
        assert job.attempts == 2

    def test_failed_job_not_dequeued(self, memory_db: Database) -> None:
        queue = JobQueue(memory_db)
        job_id = queue.enqueue("extract_entities", {}, max_attempts=1)
        queue.dequeue("extract_entities")
        queue.fail(job_id, "permanent error")

        job = queue.dequeue("extract_entities")
        assert job is None


class TestBackoff:
    def test_fail_backoff_formula(self, memory_db: Database) -> None:
        """fail() sets next_attempt_at to now + 60s * 4^(attempts-1):
        60s, 240s, 960s for attempts 1, 2, 3 (per phase0-reliability task 2).
        """
        queue = JobQueue(memory_db)
        job_id = queue.enqueue("extract_entities", {}, max_attempts=10)

        for attempts, expected_seconds in [(1, 60), (2, 240), (3, 960)]:
            conn = memory_db.connect()
            try:
                conn.execute(
                    "UPDATE jobs SET attempts = ?, status = 'running' WHERE id = ?",
                    (attempts, job_id),
                )
                conn.commit()
            finally:
                conn.close()

            before = datetime.now(UTC)
            queue.fail(job_id, "boom")

            job = queue.get_job(job_id)
            assert job is not None
            assert job.status == "pending"
            assert job.next_attempt_at is not None
            next_at = datetime.fromisoformat(job.next_attempt_at).replace(tzinfo=UTC)
            delta = (next_at - before).total_seconds()
            assert expected_seconds - 5 <= delta <= expected_seconds + 5, (
                f"attempts={attempts}: expected ~{expected_seconds}s backoff, got {delta}s"
            )

    def test_dequeue_skips_job_with_future_next_attempt_at(
        self, memory_db: Database
    ) -> None:
        queue = JobQueue(memory_db)
        job_id = queue.enqueue("extract_entities", {}, max_attempts=3)
        queue.dequeue("extract_entities")
        queue.fail(job_id, "boom")  # backoff pushes next_attempt_at into the future

        job = queue.dequeue("extract_entities")
        assert job is None

    def test_dequeue_picks_up_job_once_backoff_elapses(
        self, memory_db: Database
    ) -> None:
        queue = JobQueue(memory_db)
        job_id = queue.enqueue("extract_entities", {}, max_attempts=3)
        queue.dequeue("extract_entities")
        queue.fail(job_id, "boom")

        conn = memory_db.connect()
        try:
            conn.execute(
                "UPDATE jobs SET next_attempt_at = datetime('now', '-1 seconds') "
                "WHERE id = ?",
                (job_id,),
            )
            conn.commit()
        finally:
            conn.close()

        job = queue.dequeue("extract_entities")
        assert job is not None
        assert job.id == job_id

    def test_dequeue_treats_null_next_attempt_at_as_ready(
        self, memory_db: Database
    ) -> None:
        """Existing (pre-migration) rows have NULL next_attempt_at and must
        dequeue exactly as before."""
        queue = JobQueue(memory_db)
        job_id = queue.enqueue("extract_entities", {})
        job = queue.get_job(job_id)
        assert job is not None
        assert job.next_attempt_at is None

        dequeued = queue.dequeue("extract_entities")
        assert dequeued is not None
        assert dequeued.id == job_id


class TestRequeueFailed:
    def test_requeue_failed_resets_rows_and_reports_count(
        self, memory_db: Database
    ) -> None:
        queue = JobQueue(memory_db)
        job_id = queue.enqueue("extract_entities", {}, max_attempts=1)
        queue.dequeue("extract_entities")
        queue.fail(job_id, "permanent error")
        assert queue.get_job(job_id).status == "failed"  # type: ignore[union-attr]

        count = queue.requeue_failed()
        assert count == 1

        job = queue.get_job(job_id)
        assert job is not None
        assert job.status == "pending"
        assert job.attempts == 0
        assert job.next_attempt_at is None

    def test_requeue_failed_filters_by_type(self, memory_db: Database) -> None:
        queue = JobQueue(memory_db)
        j1 = queue.enqueue("extract_entities", {}, max_attempts=1)
        j2 = queue.enqueue("generate_summary", {}, max_attempts=1)
        queue.dequeue("extract_entities")
        queue.fail(j1, "x")
        queue.dequeue("generate_summary")
        queue.fail(j2, "x")

        count = queue.requeue_failed(job_type="extract_entities")
        assert count == 1
        assert queue.get_job(j1).status == "pending"  # type: ignore[union-attr]
        assert queue.get_job(j2).status == "failed"  # type: ignore[union-attr]

    def test_requeue_failed_ignores_requeue_count_cap(
        self, memory_db: Database
    ) -> None:
        queue = JobQueue(memory_db)
        job_id = queue.enqueue("extract_entities", {}, max_attempts=1)
        queue.dequeue("extract_entities")
        queue.fail(job_id, "x")

        conn = memory_db.connect()
        try:
            conn.execute(
                "UPDATE jobs SET requeue_count = 5 WHERE id = ?", (job_id,)
            )
            conn.commit()
        finally:
            conn.close()

        count = queue.requeue_failed()
        assert count == 1

    def test_requeue_failed_resets_requeue_count_to_zero(
        self, memory_db: Database
    ) -> None:
        """A human running `requeue-failed` is declaring the underlying
        problem fixed — the job must be eligible for auto-resurrection
        again if it later fails for a new, unrelated reason, not
        permanently capped by resurrections from before the fix."""
        queue = JobQueue(memory_db)
        job_id = queue.enqueue("extract_entities", {}, max_attempts=1)
        queue.dequeue("extract_entities")
        queue.fail(job_id, "x")

        conn = memory_db.connect()
        try:
            conn.execute(
                "UPDATE jobs SET requeue_count = 3 WHERE id = ?", (job_id,)
            )
            conn.commit()
        finally:
            conn.close()

        queue.requeue_failed()

        job = queue.get_job(job_id)
        assert job is not None
        assert job.requeue_count == 0


class TestAutoRequeueFailed:
    def test_requeues_up_to_cap_for_matching_project_via_session(
        self, memory_db: Database
    ) -> None:
        from callmem.core.engine import MemoryEngine
        from callmem.models.config import Config

        config = Config(sensitive_data={"enabled": False, "llm_scan": False})
        engine = MemoryEngine(memory_db, config)
        session = engine.start_session()

        queue = JobQueue(memory_db)

        eligible_ids = []
        for _ in range(3):
            jid = queue.enqueue(
                "extract_entities",
                {"event_ids": [], "session_id": session.id},
                max_attempts=1,
            )
            queue.dequeue("extract_entities")
            queue.fail(jid, "backend down")
            eligible_ids.append(jid)

        capped_id = queue.enqueue(
            "extract_entities",
            {"event_ids": [], "session_id": session.id},
            max_attempts=1,
        )
        queue.dequeue("extract_entities")
        queue.fail(capped_id, "backend down")
        conn = memory_db.connect()
        try:
            conn.execute(
                "UPDATE jobs SET requeue_count = 3 WHERE id = ?", (capped_id,)
            )
            conn.commit()
        finally:
            conn.close()

        requeued = queue.auto_requeue_failed("extract_entities", engine.project_id)
        assert requeued == 3

        for jid in eligible_ids:
            job = queue.get_job(jid)
            assert job is not None
            assert job.status == "pending"
            assert job.attempts == 0
            assert job.requeue_count == 1

        capped = queue.get_job(capped_id)
        assert capped is not None
        assert capped.status == "failed"

    def test_only_matches_jobs_for_the_given_project(
        self, memory_db: Database
    ) -> None:
        from callmem.core.engine import MemoryEngine
        from callmem.models.config import Config

        config_a = Config(
            project={"name": "project-a"},
            sensitive_data={"enabled": False, "llm_scan": False},
        )
        engine_a = MemoryEngine(memory_db, config_a)
        session_a = engine_a.start_session()

        config_b = Config(
            project={"name": "project-b"},
            sensitive_data={"enabled": False, "llm_scan": False},
        )
        engine_b = MemoryEngine(memory_db, config_b)
        session_b = engine_b.start_session()

        queue = JobQueue(memory_db)
        j_a = queue.enqueue(
            "extract_entities", {"session_id": session_a.id}, max_attempts=1
        )
        queue.dequeue("extract_entities")
        queue.fail(j_a, "x")

        j_b = queue.enqueue(
            "extract_entities", {"session_id": session_b.id}, max_attempts=1
        )
        queue.dequeue("extract_entities")
        queue.fail(j_b, "x")

        requeued = queue.auto_requeue_failed("extract_entities", engine_a.project_id)
        assert requeued == 1
        assert queue.get_job(j_a).status == "pending"  # type: ignore[union-attr]
        assert queue.get_job(j_b).status == "failed"  # type: ignore[union-attr]

    def test_matches_via_direct_project_id_payload(
        self, memory_db: Database
    ) -> None:
        from callmem.core.engine import MemoryEngine
        from callmem.models.config import Config

        config = Config(sensitive_data={"enabled": False, "llm_scan": False})
        engine = MemoryEngine(memory_db, config)
        project_id = engine.project_id

        queue = JobQueue(memory_db)
        job_id = queue.enqueue(
            "generate_summary", {"project_id": project_id}, max_attempts=1
        )
        queue.dequeue("generate_summary")
        queue.fail(job_id, "x")

        requeued = queue.auto_requeue_failed("generate_summary", project_id)
        assert requeued == 1
        assert queue.get_job(job_id).status == "pending"  # type: ignore[union-attr]

    def test_respects_limit_override(self, memory_db: Database) -> None:
        from callmem.core.engine import MemoryEngine
        from callmem.models.config import Config

        config = Config(sensitive_data={"enabled": False, "llm_scan": False})
        engine = MemoryEngine(memory_db, config)
        session = engine.start_session()

        queue = JobQueue(memory_db)
        for _ in range(3):
            jid = queue.enqueue(
                "extract_entities", {"session_id": session.id}, max_attempts=1
            )
            queue.dequeue("extract_entities")
            queue.fail(jid, "x")

        requeued = queue.auto_requeue_failed(
            "extract_entities", engine.project_id, limit=2
        )
        assert requeued == 2


class TestGetPendingCount:
    def test_count_reflects_pending(self, memory_db: Database) -> None:
        queue = JobQueue(memory_db)
        assert queue.get_pending_count() == 0
        queue.enqueue("extract_entities", {})
        queue.enqueue("extract_entities", {})
        assert queue.get_pending_count() == 2

    def test_count_by_type(self, memory_db: Database) -> None:
        queue = JobQueue(memory_db)
        queue.enqueue("extract_entities", {})
        queue.enqueue("generate_summary", {})
        assert queue.get_pending_count("extract_entities") == 1
        assert queue.get_pending_count("generate_summary") == 1


class TestGetFailedCount:
    def test_zero_when_no_failed_jobs(self, memory_db: Database) -> None:
        queue = JobQueue(memory_db)
        queue.enqueue("extract_entities", {})
        assert queue.get_failed_count() == 0

    def test_counts_failed_jobs(self, memory_db: Database) -> None:
        queue = JobQueue(memory_db)
        job_id = queue.enqueue("extract_entities", {}, max_attempts=1)
        queue.dequeue("extract_entities")
        queue.fail(job_id, "boom")
        assert queue.get_failed_count() == 1

    def test_does_not_count_pending_or_completed(
        self, memory_db: Database,
    ) -> None:
        queue = JobQueue(memory_db)
        queue.enqueue("extract_entities", {})
        completed_id = queue.enqueue("extract_entities", {})
        queue.dequeue("extract_entities")
        job = queue.dequeue("extract_entities")
        assert job is not None
        queue.complete(job.id)
        assert queue.get_failed_count() == 0
        assert job.id == completed_id


class TestGetLastCompletedAt:
    def test_none_when_never_completed(self, memory_db: Database) -> None:
        queue = JobQueue(memory_db)
        assert queue.get_last_completed_at("extract_entities") is None

    def test_returns_completed_at_of_most_recent(
        self, memory_db: Database,
    ) -> None:
        queue = JobQueue(memory_db)
        job_id = queue.enqueue("extract_entities", {})
        queue.dequeue("extract_entities")
        queue.complete(job_id)
        result = queue.get_last_completed_at("extract_entities")
        assert result is not None

    def test_filters_by_job_type(self, memory_db: Database) -> None:
        queue = JobQueue(memory_db)
        summary_id = queue.enqueue("generate_summary", {})
        queue.dequeue("generate_summary")
        queue.complete(summary_id)
        assert queue.get_last_completed_at("extract_entities") is None
