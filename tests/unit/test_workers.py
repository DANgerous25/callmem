"""Tests for the background worker runner."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING
from unittest.mock import patch

from callmem.core.ollama import OllamaClient
from callmem.core.queue import JobQueue
from callmem.core.workers import WorkerRunner
from callmem.models.config import Config

if TYPE_CHECKING:
    from callmem.core.database import Database
    pass


def _make_engine(memory_db: Database) -> tuple:
    from callmem.core.engine import MemoryEngine

    config = Config(
        sensitive_data={"enabled": False, "llm_scan": False},
        summarization={"chunk_size": 5, "cross_session_interval": 100},
    )
    engine = MemoryEngine(memory_db, config)
    ollama = OllamaClient()
    return engine, ollama


class TestWorkerProcessing:
    def test_process_extraction_job(self, memory_db: Database) -> None:
        engine, ollama = _make_engine(memory_db)
        engine.start_session()
        engine.ingest_one("response", "We should use cursor-based pagination")

        queue = JobQueue(memory_db)
        assert queue.get_pending_count("extract_entities") == 1

        llm_response = (
            '{"decisions": [{"title": "Use pagination", "content": "Cursor-based"}],'
            '"todos": [], "facts": [], "failures": [], "discoveries": []}'
        )
        runner = WorkerRunner(memory_db, ollama, engine.config)
        with patch.object(ollama, "_generate", return_value=llm_response):
            processed = runner.process_one()

        assert processed is True
        assert queue.get_pending_count("extract_entities") == 0

    def test_process_summarization_job(self, memory_db: Database) -> None:
        engine, ollama = _make_engine(memory_db)
        session = engine.start_session()
        engine.ingest_one("note", "some content")
        engine.end_session(session.id)

        queue = JobQueue(memory_db)
        assert queue.get_pending_count("generate_summary") >= 1

        runner = WorkerRunner(memory_db, ollama, engine.config)
        with patch.object(
            ollama, "_generate", return_value="Summary of the session."
        ):
            processed = runner.process_one()

        assert processed is True

    def test_process_compaction_job(self, memory_db: Database) -> None:
        engine, ollama = _make_engine(memory_db)
        session = engine.start_session()
        engine.ingest_one("note", "some content")
        engine.end_session(session.id)

        queue = JobQueue(memory_db)
        runner = WorkerRunner(memory_db, ollama, engine.config)

        with patch.object(
            ollama, "_generate", return_value="Summary."
        ):
            while queue.get_pending_count("generate_summary") > 0:
                runner.process_one()

        assert queue.get_pending_count("compact") >= 1

        processed = runner.process_one()
        assert processed is True

        compact_jobs_remaining = queue.get_pending_count("compact")
        assert compact_jobs_remaining == 0

    def test_empty_queue_returns_false(self, memory_db: Database) -> None:
        engine, ollama = _make_engine(memory_db)
        runner = WorkerRunner(memory_db, ollama, engine.config)
        result = runner.process_one()
        assert result is False


class TestWorkerUnknownJob:
    def test_unknown_job_type_handled(self, memory_db: Database) -> None:
        engine, ollama = _make_engine(memory_db)
        queue = JobQueue(memory_db)
        queue.enqueue("unknown_type", {"data": "test"}, max_attempts=1)

        runner = WorkerRunner(memory_db, ollama, engine.config)
        processed = runner.process_one()
        assert processed is True

        conn = memory_db.connect()
        try:
            row = conn.execute(
                "SELECT * FROM jobs WHERE type = 'unknown_type' LIMIT 1"
            ).fetchone()
            assert row is not None
            assert row["status"] == "failed"
        finally:
            conn.close()


class TestWorkerRetry:
    def test_failed_job_retried(self, memory_db: Database) -> None:
        engine, ollama = _make_engine(memory_db)
        engine.start_session()
        engine.ingest_one("response", "test content")

        runner = WorkerRunner(memory_db, ollama, engine.config)
        with patch.object(ollama, "_generate", return_value=None):
            runner.process_one()

        conn = memory_db.connect()
        try:
            row = conn.execute(
                "SELECT * FROM jobs WHERE type = 'extract_entities' LIMIT 1"
            ).fetchone()
            assert row is not None
            assert row["attempts"] >= 1
        finally:
            conn.close()


class TestClaimedJobPayloadProcessed:
    """Regression tests: process_one() must process the payload of the job
    it just claimed, not only drain other still-pending jobs of that type.
    """

    def test_single_extraction_job_processes_claimed_payload(
        self, memory_db: Database
    ) -> None:
        engine, ollama = _make_engine(memory_db)
        engine.start_session()
        engine.ingest_one("response", "We should use cursor-based pagination")

        queue = JobQueue(memory_db)
        assert queue.get_pending_count("extract_entities") == 1

        llm_response = (
            '{"decisions": [{"title": "Use pagination", "content": "Cursor-based"}],'
            '"todos": [], "facts": [], "failures": [], "discoveries": []}'
        )
        runner = WorkerRunner(memory_db, ollama, engine.config)
        with patch.object(ollama, "_generate", return_value=llm_response):
            processed = runner.process_one()

        assert processed is True

        conn = memory_db.connect()
        try:
            entity_count = conn.execute(
                "SELECT COUNT(*) as c FROM entities"
            ).fetchone()["c"]
            job_row = conn.execute(
                "SELECT status FROM jobs WHERE type = 'extract_entities' LIMIT 1"
            ).fetchone()
        finally:
            conn.close()

        assert entity_count >= 1, (
            "claimed job's payload was never processed — 0 entities created"
        )
        assert job_row["status"] == "completed"

    def test_single_summarization_job_processes_claimed_payload(
        self, memory_db: Database
    ) -> None:
        engine, ollama = _make_engine(memory_db)
        session = engine.start_session()
        event = engine.ingest_one("note", "some content")
        assert event is not None

        queue = JobQueue(memory_db)
        # ingest_one already queued an extract_entities job — drain it first
        # so the generate_summary job below is the only pending job, and
        # process_one() is guaranteed to claim *that* one.
        extraction_job = queue.dequeue("extract_entities")
        assert extraction_job is not None
        queue.complete(extraction_job.id)

        summary_job_id = queue.enqueue(
            "generate_summary",
            {
                "level": "chunk",
                "project_id": engine.project_id,
                "session_id": session.id,
                "event_ids": [event.id],
            },
        )

        runner = WorkerRunner(memory_db, ollama, engine.config)
        with patch.object(
            ollama, "_generate", return_value="Summary of the session."
        ):
            processed = runner.process_one()

        assert processed is True

        conn = memory_db.connect()
        try:
            summary_count = conn.execute(
                "SELECT COUNT(*) as c FROM summaries"
            ).fetchone()["c"]
            job_row = conn.execute(
                "SELECT status FROM jobs WHERE id = ?", (summary_job_id,)
            ).fetchone()
        finally:
            conn.close()

        assert summary_count >= 1, (
            "claimed job's payload was never processed — 0 summaries created"
        )
        assert job_row["status"] == "completed"

    def test_multiple_pending_extraction_jobs_all_processed(
        self, memory_db: Database
    ) -> None:
        engine, ollama = _make_engine(memory_db)
        engine.start_session()
        engine.ingest_one("response", "Decision one about caching")
        engine.ingest_one("response", "Decision two about pagination")
        engine.ingest_one("response", "Decision three about auth")

        queue = JobQueue(memory_db)
        assert queue.get_pending_count("extract_entities") == 3

        llm_response = (
            '{"decisions": [{"title": "A decision", "content": "content"}],'
            '"todos": [], "facts": [], "failures": [], "discoveries": []}'
        )
        runner = WorkerRunner(memory_db, ollama, engine.config)
        with patch.object(ollama, "_generate", return_value=llm_response):
            processed = runner.process_one()

        assert processed is True
        assert queue.get_pending_count("extract_entities") == 0

        conn = memory_db.connect()
        try:
            entity_count = conn.execute(
                "SELECT COUNT(*) as c FROM entities"
            ).fetchone()["c"]
        finally:
            conn.close()

        assert entity_count == 3, (
            "expected all 3 jobs (claimed + drained) to produce entities"
        )

    def test_claimed_job_failure_marks_failed_not_completed(
        self, memory_db: Database
    ) -> None:
        engine, ollama = _make_engine(memory_db)
        engine.start_session()
        engine.ingest_one("response", "some content")

        runner = WorkerRunner(memory_db, ollama, engine.config)
        with patch.object(
            ollama, "_generate", side_effect=RuntimeError("boom")
        ):
            processed = runner.process_one()

        assert processed is True

        conn = memory_db.connect()
        try:
            job_row = conn.execute(
                "SELECT status FROM jobs WHERE type = 'extract_entities' LIMIT 1"
            ).fetchone()
        finally:
            conn.close()

        assert job_row["status"] != "completed", (
            "claimed job must go through queue.fail on error, not complete"
        )


class TestWorkerThread:
    def test_start_and_stop(self, memory_db: Database) -> None:
        engine, ollama = _make_engine(memory_db)
        runner = WorkerRunner(memory_db, ollama, engine.config, poll_interval=1)

        runner.start()
        assert runner._thread is not None
        assert runner._thread.is_alive()

        time.sleep(0.5)
        runner.stop()
        assert not runner._thread.is_alive()

    def test_worker_thread_is_daemon(self, memory_db: Database) -> None:
        engine, ollama = _make_engine(memory_db)
        runner = WorkerRunner(memory_db, ollama, engine.config)

        runner.start()
        assert runner._thread is not None
        assert runner._thread.daemon is True
        runner.stop()
