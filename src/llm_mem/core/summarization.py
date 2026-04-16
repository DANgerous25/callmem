"""Layered summarization: chunk, session, and cross-session.

Summarization uses the job queue. The Summarizer dequeues jobs,
calls Ollama to generate summaries, and stores them in the summaries table.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from llm_mem.core.prompts import (
    CHUNK_SUMMARY_PROMPT,
    CROSS_SESSION_PROMPT,
    SESSION_SUMMARY_PROMPT,
)
from llm_mem.core.queue import JobQueue
from llm_mem.core.retrieval import _estimate_tokens
from llm_mem.models.summaries import Summary

if TYPE_CHECKING:
    from llm_mem.core.database import Database
    from llm_mem.core.ollama import OllamaClient

logger = logging.getLogger(__name__)


class Summarizer:
    """Generates chunk, session, and cross-session summaries."""

    def __init__(self, db: Database, ollama: OllamaClient) -> None:
        self.db = db
        self.ollama = ollama
        self.queue = JobQueue(db)

    def process_pending(self) -> list[Summary]:
        """Process all pending summarization jobs."""
        all_summaries: list[Summary] = []

        while True:
            job = self.queue.dequeue("generate_summary")
            if job is None:
                break

            try:
                summary = self._process_job(job)
                if summary is not None:
                    all_summaries.append(summary)
                self.queue.complete(job.id)
            except Exception as exc:
                logger.error("Summarization job %s failed: %s", job.id, exc)
                self.queue.fail(job.id, str(exc))

        return all_summaries

    def _process_job(self, job: Any) -> Summary | None:
        payload = job.payload
        level = payload.get("level", "chunk")

        if level == "chunk":
            return self._generate_chunk_summary(payload)
        elif level == "session":
            return self._generate_session_summary(payload)
        elif level == "cross_session":
            return self._generate_cross_session_summary(payload)
        return None

    def _generate_chunk_summary(self, payload: dict[str, Any]) -> Summary | None:
        event_ids = payload.get("event_ids", [])
        project_id = payload.get("project_id", "")
        session_id = payload.get("session_id")

        events = self._fetch_events(event_ids)
        if not events:
            return None

        events_text = "\n".join(
            f"[{e['type']}] {e['content']}" for e in events
        )
        prompt = CHUNK_SUMMARY_PROMPT.format(events_text=events_text)
        response = self.ollama._generate(prompt)
        if response is None:
            raise RuntimeError("Ollama returned no response for chunk summary")

        token_count = _estimate_tokens(response)
        first_ts = events[0].get("timestamp")
        last_ts = events[-1].get("timestamp")

        summary = Summary(
            project_id=project_id,
            session_id=session_id,
            level="chunk",
            content=response,
            event_range_start=first_ts,
            event_range_end=last_ts,
            event_count=len(events),
            token_count=token_count,
        )
        self._insert_summary(summary)
        return summary

    def _generate_session_summary(
        self, payload: dict[str, Any]
    ) -> Summary | None:
        project_id = payload.get("project_id", "")
        session_id = payload.get("session_id")

        chunk_summaries = self._fetch_chunk_summaries(session_id)
        chunks_text = "\n".join(s["content"] for s in chunk_summaries)

        remaining_events = self._fetch_unsummarized_events(session_id)
        remaining_text = "\n".join(
            f"[{e['type']}] {e['content']}" for e in remaining_events
        )

        prompt = SESSION_SUMMARY_PROMPT.format(
            chunks_text=chunks_text or "None",
            remaining_events_text=remaining_text or "None",
        )
        response = self.ollama._generate(prompt)
        if response is None:
            raise RuntimeError("Ollama returned no response for session summary")

        token_count = _estimate_tokens(response)

        summary = Summary(
            project_id=project_id,
            session_id=session_id,
            level="session",
            content=response,
            event_count=len(chunk_summaries) + len(remaining_events),
            token_count=token_count,
        )
        self._insert_summary(summary)
        return summary

    def _generate_cross_session_summary(
        self, payload: dict[str, Any]
    ) -> Summary | None:
        project_id = payload.get("project_id", "")
        max_tokens = payload.get("max_tokens", 2000)

        session_summaries = self._fetch_session_summaries(project_id)
        if not session_summaries:
            return None

        sessions_text = "\n\n".join(
            f"Session ({s.get('session_id', 'unknown')[:8]}):\n{s['content']}"
            for s in session_summaries
        )

        prompt = CROSS_SESSION_PROMPT.format(
            sessions_text=sessions_text,
            max_tokens=max_tokens,
        )
        response = self.ollama._generate(prompt)
        if response is None:
            raise RuntimeError("Ollama returned no response for cross-session summary")

        token_count = _estimate_tokens(response)

        summary = Summary(
            project_id=project_id,
            level="cross_session",
            content=response,
            token_count=token_count,
        )
        self._insert_summary(summary)
        return summary

    def _fetch_events(
        self, event_ids: list[str]
    ) -> list[dict[str, Any]]:
        if not event_ids:
            return []
        conn = self.db.connect()
        try:
            placeholders = ",".join("?" for _ in event_ids)
            rows = conn.execute(
                f"SELECT * FROM events WHERE id IN ({placeholders}) "
                f"ORDER BY timestamp ASC",
                event_ids,
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def _fetch_chunk_summaries(
        self, session_id: str | None
    ) -> list[dict[str, Any]]:
        if not session_id:
            return []
        conn = self.db.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM summaries "
                "WHERE session_id = ? AND level = 'chunk' "
                "ORDER BY created_at ASC",
                (session_id,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def _fetch_unsummarized_events(
        self, session_id: str | None
    ) -> list[dict[str, Any]]:
        if not session_id:
            return []
        conn = self.db.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM events "
                "WHERE session_id = ? "
                "AND id NOT IN ("
                "  SELECT event_range_start FROM summaries "
                "  WHERE session_id = ? AND level = 'chunk' AND event_range_start IS NOT NULL"
                ") "
                "ORDER BY timestamp ASC",
                (session_id, session_id),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def _fetch_session_summaries(
        self, project_id: str
    ) -> list[dict[str, Any]]:
        conn = self.db.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM summaries "
                "WHERE project_id = ? AND level = 'session' "
                "ORDER BY created_at DESC LIMIT 10",
                (project_id,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def _insert_summary(self, summary: Summary) -> None:
        conn = self.db.connect()
        try:
            row = summary.to_row()
            conn.execute(
                "INSERT INTO summaries "
                "(id, project_id, session_id, level, content, "
                "event_range_start, event_range_end, event_count, "
                "token_count, created_at, metadata) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    row["id"], row["project_id"], row["session_id"],
                    row["level"], row["content"],
                    row["event_range_start"], row["event_range_end"],
                    row["event_count"], row["token_count"],
                    row["created_at"], row["metadata"],
                ),
            )
            conn.commit()
        finally:
            conn.close()
