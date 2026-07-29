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
    next_attempt_at: str | None
    requeue_count: int

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
            next_attempt_at=row["next_attempt_at"],
            requeue_count=row["requeue_count"],
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
                    "  AND (next_attempt_at IS NULL OR next_attempt_at <= datetime('now')) "
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
                    "  AND (next_attempt_at IS NULL OR next_attempt_at <= datetime('now')) "
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
        """Mark a job as failed. If under max_attempts, reset to pending for retry.

        A retried job gets an exponential backoff `next_attempt_at`
        (60s * 4^(attempts-1): 60s, 240s, 960s, ...) so dequeue() won't
        re-claim it immediately — without this, all retries burn within
        seconds of a backend outage and the job dies permanently before
        the backend has any chance to recover.
        """
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
                delay_seconds = 60 * (4 ** max(attempts - 1, 0))
                conn.execute(
                    "UPDATE jobs SET status = 'pending', error = ?, "
                    "next_attempt_at = datetime('now', ?) WHERE id = ?",
                    (error, f"+{delay_seconds} seconds", job_id),
                )
            else:
                conn.execute(
                    "UPDATE jobs SET status = 'failed', error = ? WHERE id = ?",
                    (error, job_id),
                )
            conn.commit()
        finally:
            conn.close()

    def requeue_failed(self, job_type: str | None = None) -> int:
        """Reset `failed` jobs back to `pending`, ignoring the requeue_count
        cap. Manual recovery path backing `callmem requeue-failed`.

        Also resets requeue_count to 0: a human running this command is
        declaring the underlying problem fixed, so these jobs must be
        eligible for auto-resurrection again if they fail later for a new
        reason — not permanently capped by resurrections from before the
        fix.

        Returns the number of rows reset.
        """
        conn = self.db.connect()
        try:
            if job_type is not None:
                cur = conn.execute(
                    "UPDATE jobs SET status = 'pending', attempts = 0, "
                    "next_attempt_at = NULL, requeue_count = 0 "
                    "WHERE status = 'failed' AND type = ?",
                    (job_type,),
                )
            else:
                cur = conn.execute(
                    "UPDATE jobs SET status = 'pending', attempts = 0, "
                    "next_attempt_at = NULL, requeue_count = 0 "
                    "WHERE status = 'failed'"
                )
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()

    def auto_requeue_failed(
        self,
        job_type: str,
        project_id: str,
        limit: int = 50,
        max_requeue_count: int = 3,
    ) -> int:
        """Auto-resurrect `failed` jobs of `job_type` belonging to `project_id`.

        Called when a same-type job just completed successfully — proof the
        backend is healthy again. Requeues oldest-first, up to `limit` jobs,
        skipping any whose requeue_count already hit `max_requeue_count`
        (this is what bounds the retry loop; `requeue_failed` above has no
        such cap). A job's project is resolved either from a `project_id`
        key in its own payload (e.g. generate_summary jobs) or, failing
        that, via its `session_id` payload key joined against `sessions`
        (e.g. extract_entities jobs).

        Returns the number of rows requeued.
        """
        conn = self.db.connect()
        try:
            cur = conn.execute(
                "UPDATE jobs SET status = 'pending', attempts = 0, "
                "next_attempt_at = NULL, requeue_count = requeue_count + 1 "
                "WHERE id IN ("
                "  SELECT j.id FROM jobs j "
                "  LEFT JOIN sessions s "
                "    ON s.id = json_extract(j.payload, '$.session_id') "
                "  WHERE j.status = 'failed' AND j.type = ? "
                "  AND j.requeue_count < ? "
                "  AND (json_extract(j.payload, '$.project_id') = ? "
                "       OR s.project_id = ?) "
                "  ORDER BY j.created_at ASC LIMIT ?"
                ")",
                (job_type, max_requeue_count, project_id, project_id, limit),
            )
            conn.commit()
            return cur.rowcount
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

    def get_failed_count(self) -> int:
        """Return the total number of jobs currently in 'failed' status.

        Used by the briefing's pipeline health check — a large failed
        count means a backend outage is silently piling up dead jobs.
        """
        conn = self.db.connect()
        try:
            row = conn.execute(
                "SELECT COUNT(*) as c FROM jobs WHERE status = 'failed'"
            ).fetchone()
            return row["c"]
        finally:
            conn.close()

    def get_last_completed_at(self, job_type: str) -> str | None:
        """Return the `completed_at` of the most recently completed job of
        `job_type`, or None if no such job has ever completed.

        Used by the briefing's pipeline health check to detect a stalled
        extraction pipeline (events flowing in but nothing being extracted).
        """
        conn = self.db.connect()
        try:
            row = conn.execute(
                "SELECT completed_at FROM jobs "
                "WHERE status = 'completed' AND type = ? "
                "ORDER BY completed_at DESC LIMIT 1",
                (job_type,),
            ).fetchone()
            return row["completed_at"] if row else None
        finally:
            conn.close()
