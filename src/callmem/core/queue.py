"""SQLite-backed job queue for background processing.

No external dependencies (no Redis, no Celery).
Jobs are stored in the `jobs` table and processed by workers.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ulid import ULID

if TYPE_CHECKING:
    from callmem.core.database import Database

logger = logging.getLogger(__name__)


@dataclass
class Job:
    """A single background job."""

    id: str
    type: str
    payload: dict[str, Any]
    status: str
    attempts: int
    max_attempts: int
    created_at: str
    started_at: str | None
    completed_at: str | None
    error: str | None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> Job:
        payload = row["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        return cls(
            id=row["id"],
            type=row["type"],
            payload=payload,
            status=row["status"],
            attempts=row["attempts"],
            max_attempts=row["max_attempts"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            error=row["error"],
        )


class JobQueue:
    """SQLite-backed job queue for background work."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def enqueue(
        self,
        job_type: str,
        payload: dict[str, Any],
        max_attempts: int = 3,
    ) -> str:
        """Add a job to the queue. Returns the job ID."""
        job_id = str(ULID())
        conn = self.db.connect()
        try:
            conn.execute(
                "INSERT INTO jobs "
                "(id, type, payload, status, attempts, max_attempts, created_at) "
                "VALUES (?, ?, ?, 'pending', 0, ?, datetime('now'))",
                (job_id, job_type, json.dumps(payload), max_attempts),
            )
            conn.commit()
        finally:
            conn.close()
        return job_id

    def dequeue(self, job_type: str | None = None) -> Job | None:
        """Claim the next pending job, optionally filtered by type.

        Sets status to 'running' and increments attempts.
        Uses a single atomic UPDATE with RETURNING so concurrent workers
        never claim the same job. Returns None if no jobs are available.
        """
        conn = self.db.connect()
        try:
            if job_type is not None:
                row = conn.execute(
                    "UPDATE jobs SET status = 'running', "
                    "started_at = datetime('now'), "
                    "attempts = attempts + 1 "
                    "WHERE id = ("
                    "  SELECT id FROM jobs "
                    "  WHERE status = 'pending' AND type = ? "
                    "  ORDER BY created_at ASC LIMIT 1"
                    ") RETURNING *",
                    (job_type,),
                ).fetchone()
            else:
                row = conn.execute(
                    "UPDATE jobs SET status = 'running', "
                    "started_at = datetime('now'), "
                    "attempts = attempts + 1 "
                    "WHERE id = ("
                    "  SELECT id FROM jobs "
                    "  WHERE status = 'pending' "
                    "  ORDER BY created_at ASC LIMIT 1"
                    ") RETURNING *",
                ).fetchone()

            if row is None:
                return None

            conn.commit()
            return Job.from_row(dict(row))
        finally:
            conn.close()

    def complete(self, job_id: str) -> None:
        """Mark a job as completed."""
        conn = self.db.connect()
        try:
            conn.execute(
                "UPDATE jobs SET status = 'completed', completed_at = datetime('now') "
                "WHERE id = ?",
                (job_id,),
            )
            conn.commit()
        finally:
            conn.close()

    def fail(self, job_id: str, error: str) -> None:
        """Mark a job as failed. If under max_attempts, reset to pending for retry."""
        conn = self.db.connect()
        try:
            job_row = conn.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if job_row is None:
                return

            attempts = job_row["attempts"]
            max_attempts = job_row["max_attempts"]

            if attempts < max_attempts:
                conn.execute(
                    "UPDATE jobs SET status = 'pending', error = ? WHERE id = ?",
                    (error, job_id),
                )
            else:
                conn.execute(
                    "UPDATE jobs SET status = 'failed', error = ? WHERE id = ?",
                    (error, job_id),
                )
            conn.commit()
        finally:
            conn.close()

    def get_pending_count(self, job_type: str | None = None) -> int:
        """Return the number of pending jobs."""
        conn = self.db.connect()
        try:
            if job_type is not None:
                row = conn.execute(
                    "SELECT COUNT(*) as c FROM jobs WHERE status = 'pending' AND type = ?",
                    (job_type,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*) as c FROM jobs WHERE status = 'pending'"
                ).fetchone()
            return row["c"]
        finally:
            conn.close()

    def get_job(self, job_id: str) -> Job | None:
        """Get a job by ID."""
        conn = self.db.connect()
        try:
            row = conn.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None:
                return None
            return Job.from_row(dict(row))
        finally:
            conn.close()

    def reap_orphaned_running(self, stale_after_seconds: int = 300) -> int:
        """Recover jobs stuck in 'running' after a daemon crash/restart.

        Workers mark a job 'running' when they dequeue it and only flip it
        to 'completed' or 'failed' when they're done. If the daemon is
        killed mid-inference, the job stays 'running' forever and nothing
        else picks it up.

        This is safe because the queue is single-writer per DB: only one
        callmem daemon ever points at a given .callmem/memory.db, so any
        'running' row older than ``stale_after_seconds`` is ours and it's
        orphaned. We push it back to 'pending' (or to 'failed' once it
        has burned through max_attempts) so the next dequeue picks it up.
        """
        conn = self.db.connect()
        try:
            rows = conn.execute(
                "SELECT id, attempts, max_attempts FROM jobs "
                "WHERE status = 'running' "
                "AND (started_at IS NULL "
                "     OR started_at <= datetime('now', ?))",
                (f"-{int(stale_after_seconds)} seconds",),
            ).fetchall()

            reaped = 0
            for r in rows:
                if r["attempts"] >= r["max_attempts"]:
                    conn.execute(
                        "UPDATE jobs SET status = 'failed', "
                        "error = 'orphaned — daemon died mid-run, retries exhausted' "
                        "WHERE id = ?",
                        (r["id"],),
                    )
                else:
                    conn.execute(
                        "UPDATE jobs SET status = 'pending', "
                        "started_at = NULL "
                        "WHERE id = ?",
                        (r["id"],),
                    )
                reaped += 1
            conn.commit()
            if reaped:
                logger.info(
                    "Reaped %d orphaned 'running' job(s) on startup", reaped,
                )
            return reaped
        finally:
            conn.close()

    def get_status_summary(self) -> dict[str, int]:
        """Return counts by status."""
        conn = self.db.connect()
        try:
            rows = conn.execute(
                "SELECT status, COUNT(*) as c FROM jobs GROUP BY status"
            ).fetchall()
            counts: dict[str, int] = {
                "pending": 0, "running": 0, "completed": 0, "failed": 0,
            }
            for r in rows:
                counts[r["status"]] = r["c"]
            return counts
        finally:
            conn.close()
