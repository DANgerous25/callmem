"""Tests for the summarization system."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

from llm_mem.core.ollama import OllamaClient
from llm_mem.core.queue import JobQueue
from llm_mem.core.summarization import Summarizer
from llm_mem.models.config import Config

if TYPE_CHECKING:
    from llm_mem.core.database import Database
    pass


def _make_engine(memory_db: Database) -> tuple:
    from llm_mem.core.engine import MemoryEngine

    config = Config(
        sensitive_data={"enabled": False, "llm_scan": False},
        summarization={"chunk_size": 5, "cross_session_interval": 2},
    )
    engine = MemoryEngine(memory_db, config)
    ollama = OllamaClient()
    summarizer = Summarizer(memory_db, ollama)
    return engine, summarizer


class TestChunkSummary:
    def test_chunk_summary_triggered_on_threshold(
        self, memory_db: Database
    ) -> None:
        engine, summarizer = _make_engine(memory_db)
        engine.start_session()

        for i in range(5):
            engine.ingest_one("note", f"Event {i}: worked on pagination")

        assert JobQueue(memory_db).get_pending_count("generate_summary") >= 1

        llm_response = "Implemented pagination for the query endpoint."
        with patch.object(summarizer.ollama, "_generate", return_value=llm_response):
            summaries = summarizer.process_pending()

        chunk_summaries = [s for s in summaries if s.level == "chunk"]
        assert len(chunk_summaries) == 1
        assert chunk_summaries[0].content == llm_response
        assert chunk_summaries[0].event_count == 5

    def test_chunk_summary_has_event_range(
        self, memory_db: Database
    ) -> None:
        engine, summarizer = _make_engine(memory_db)
        engine.start_session()

        for i in range(5):
            engine.ingest_one("note", f"Event {i}")

        with patch.object(
            summarizer.ollama, "_generate", return_value="Summary text"
        ):
            summaries = summarizer.process_pending()

        chunk = [s for s in summaries if s.level == "chunk"]
        assert len(chunk) == 1
        assert chunk[0].event_range_start is not None
        assert chunk[0].event_range_end is not None

    def test_chunk_summary_has_token_count(
        self, memory_db: Database
    ) -> None:
        engine, summarizer = _make_engine(memory_db)
        engine.start_session()

        for i in range(5):
            engine.ingest_one("note", f"Event {i}")

        with patch.object(
            summarizer.ollama, "_generate", return_value="Summary text"
        ):
            summaries = summarizer.process_pending()

        chunk = [s for s in summaries if s.level == "chunk"]
        assert len(chunk) == 1
        assert chunk[0].token_count is not None
        assert chunk[0].token_count > 0

    def test_no_chunk_before_threshold(
        self, memory_db: Database
    ) -> None:
        engine, summarizer = _make_engine(memory_db)
        engine.start_session()

        for i in range(3):
            engine.ingest_one("note", f"Event {i}")

        queue = JobQueue(memory_db)
        assert queue.get_pending_count("generate_summary") == 0


class TestSessionSummary:
    def test_session_summary_on_end(self, memory_db: Database) -> None:
        engine, summarizer = _make_engine(memory_db)
        session = engine.start_session()
        engine.ingest_one("note", "Add pagination")
        engine.ingest_one("note", "Done, using cursor approach")
        engine.end_session(session.id)

        queue = JobQueue(memory_db)
        assert queue.get_pending_count("generate_summary") >= 1

        llm_response = "Session focused on adding cursor-based pagination."
        with patch.object(summarizer.ollama, "_generate", return_value=llm_response):
            summaries = summarizer.process_pending()

        session_summaries = [s for s in summaries if s.level == "session"]
        assert len(session_summaries) == 1
        assert session_summaries[0].content == llm_response

    def test_session_summary_stored_in_fts(
        self, memory_db: Database
    ) -> None:
        engine, summarizer = _make_engine(memory_db)
        session = engine.start_session()
        engine.ingest_one("note", "some event")
        engine.end_session(session.id)

        with patch.object(
            summarizer.ollama, "_generate",
            return_value="Implemented Redis caching layer for auth.",
        ):
            summarizer.process_pending()

        conn = memory_db.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM summaries_fts WHERE summaries_fts MATCH 'Redis caching'"
            ).fetchall()
            assert len(rows) > 0
        finally:
            conn.close()


class TestCrossSessionSummary:
    def test_cross_session_summary_triggered(
        self, memory_db: Database
    ) -> None:
        engine, summarizer = _make_engine(memory_db)

        for sess_num in range(2):
            session = engine.start_session()
            engine.ingest_one("note", f"Session {sess_num} work")
            engine.end_session(session.id)

        queue = JobQueue(memory_db)
        assert queue.get_pending_count("generate_summary") >= 1

        with patch.object(
            summarizer.ollama, "_generate",
            return_value="Project overview covering 2 sessions.",
        ):
            summaries = summarizer.process_pending()

        cross = [s for s in summaries if s.level == "cross_session"]
        assert len(cross) == 1


class TestOllamaUnavailable:
    def test_jobs_queue_when_ollama_down(
        self, memory_db: Database
    ) -> None:
        engine, summarizer = _make_engine(memory_db)
        session = engine.start_session()
        for i in range(5):
            engine.ingest_one("note", f"Event {i}")
        engine.end_session(session.id)

        with patch.object(
            summarizer.ollama, "_generate", return_value=None
        ):
            summaries = summarizer.process_pending()

        assert summaries == []

        queue = JobQueue(memory_db)
        assert queue.get_pending_count("generate_summary") == 0

        conn = memory_db.connect()
        try:
            failed = conn.execute(
                "SELECT COUNT(*) as c FROM jobs "
                "WHERE type = 'generate_summary' AND status = 'failed'"
            ).fetchone()
            assert failed["c"] > 0
        finally:
            conn.close()
