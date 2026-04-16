"""Tests for the SQLite-backed job queue."""

from __future__ import annotations

from typing import TYPE_CHECKING

from llm_mem.core.queue import JobQueue

if TYPE_CHECKING:
    from llm_mem.core.database import Database


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
