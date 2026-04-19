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
