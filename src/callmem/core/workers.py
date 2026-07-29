"""Background worker runner — processes the job queue.

Dispatches extraction, summarization, and compaction jobs.
Runs in a background thread alongside the MCP server.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING, Any

from callmem.core.compaction import Compactor
from callmem.core.embeddings import EMBED_JOB_TYPE, EntityEmbedder
from callmem.core.extraction import EntityExtractor
from callmem.core.queue import JobQueue
from callmem.core.staleness import StalenessChecker
from callmem.core.summarization import Summarizer

if TYPE_CHECKING:
    from callmem.core.database import Database
    from callmem.core.ollama import OllamaClient
    from callmem.models.config import Config

logger = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL = 5


class WorkerRunner:
    """Processes background jobs from the queue in a daemon thread."""

    def __init__(
        self,
        db: Database,
        ollama: OllamaClient,
        config: Config,
        poll_interval: int = DEFAULT_POLL_INTERVAL,
        event_bus: Any | None = None,
        project_path: str | None = None,
    ) -> None:
        self.db = db
        self.ollama = ollama
        self.config = config
        self.queue = JobQueue(db)
        self.poll_interval = poll_interval
        self.running = False
        self._thread: threading.Thread | None = None
        self.event_bus = event_bus
        self.project_path = project_path
        self._extractions_since_summary = 0

        self._handlers: dict[str, Any] = {
            "extract_entities": EntityExtractor(
                db, ollama, event_bus, config=config,
            ),
            "generate_summary": Summarizer(db, ollama),
            "compact": Compactor(db, config),
            "staleness_check": StalenessChecker(db, ollama),
            EMBED_JOB_TYPE: EntityEmbedder(db, config),
        }

    def start(self) -> None:
        """Start the worker loop in a background daemon thread."""
        # Recover jobs left in 'running' from a prior daemon that died
        # mid-inference (e.g. systemctl restart during an Ollama call).
        # Without this they sit frozen forever because dequeue() only
        # picks up 'pending' rows.
        try:
            reaped = self.queue.reap_orphaned_running()
            if reaped:
                self._publish_queue_status()
        except Exception as exc:  # noqa: BLE001 — reaper must never block startup
            logger.warning("Orphaned-job reaper failed: %s", exc)

        self.running = True
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="callmem-worker"
        )
        self._thread.start()
        logger.info("Worker runner started (poll_interval=%ds)", self.poll_interval)

    def stop(self) -> None:
        """Signal the worker to stop and wait for the current job."""
        self.running = False
        if self._thread is not None:
            self._thread.join(timeout=30)
        logger.info("Worker runner stopped")

    def process_one(self) -> bool:
        """Process a single pending job synchronously.

        Returns True if a job was processed, False if queue was empty.
        """
        job = self.queue.dequeue()
        if job is None:
            return False

        handler = self._handlers.get(job.type)
        if handler is None:
            self.queue.fail(job.id, f"Unknown job type: {job.type}")
            logger.warning("Unknown job type: %s", job.type)
            return True

        logger.info("Processing job %s (type=%s, attempt=%d)", job.id[:8], job.type, job.attempts)
        try:
            self._dispatch(handler, job)
            self.queue.complete(job.id)
            logger.info("Job %s completed", job.id[:8])
            self._publish_queue_status()
            if job.type in (
                "extract_entities", "generate_summary", EMBED_JOB_TYPE,
            ):
                self._auto_resurrect_failed(job)
            if job.type == "extract_entities":
                self._extractions_since_summary += 1
                if self._extractions_since_summary >= 5:
                    self._maybe_write_session_summary()
                    self._extractions_since_summary = 0
                self._enqueue_staleness_check(job)
        except Exception as exc:
            logger.error("Job %s failed: %s", job.id[:8], exc)
            self.queue.fail(job.id, str(exc))
            self._publish_queue_status()
            if job.type == "extract_entities":
                self._log_extraction_failure(job, exc)

        return True

    def _dispatch(self, handler: Any, job: Any) -> None:
        """Dispatch a job to the appropriate handler method.

        For EntityExtractor/Summarizer/EntityEmbedder, the claimed job's own
        payload is processed directly first — process_one owns that job's
        complete/fail. A fault here must propagate so process_one can fail
        the claimed job.

        process_pending() then drains any other jobs still pending, in its
        own try/except: a fault during the drain (e.g. the queue's dequeue
        call itself raising under contention) must never be attributed to
        the claimed job, which may have already completed successfully and
        is not safe to reprocess — extraction/summarization inserts are not
        idempotent. Any still-pending jobs the drain didn't reach are simply
        picked up on the next tick.

        Embedding jobs are idempotent (``upsert_embedding`` + an
        already-embedded skip), but they follow the same two-phase shape
        so a drain fault is never charged to the claimed job.
        """
        if isinstance(handler, (EntityExtractor, Summarizer, EntityEmbedder)):
            handler.process_job(job)
            try:
                handler.process_pending()
            except Exception as exc:
                logger.error(
                    "Drain phase failed after claimed job %s (type=%s) "
                    "already succeeded: %s", job.id[:8], job.type, exc,
                )
        elif isinstance(handler, (Compactor, StalenessChecker)):
            project_id = job.payload.get("project_id", "")
            handler.run(project_id)
        else:
            raise RuntimeError(f"No dispatch for handler: {type(handler)}")

    def _publish_queue_status(self) -> None:
        """Publish queue status via event_bus if available."""
        if self.event_bus is not None:
            try:
                counts = self.queue.get_status_summary()
                self.event_bus.publish("queue_updated", counts)
            except Exception as exc:
                logger.warning("Failed to publish queue status: %s", exc)

    def _maybe_write_session_summary(self) -> None:
        """Write SESSION_SUMMARY.md if auto-write is enabled and project_path is set."""
        if not self.project_path:
            return
        if not self.config.briefing.auto_write_session_summary:
            return
        try:
            from callmem.core.briefing import BriefingGenerator
            from callmem.core.repository import Repository

            repo = Repository(self.db)
            project_name = self.config.project.name or "default"
            project = repo.get_project_by_name(project_name)
            if project is None:
                logger.warning("No project '%s' found, skipping summary", project_name)
                return
            gen = BriefingGenerator(repo, self.config, self.ollama)
            gen.write_session_summary(
                project_id=project.id,
                project_name=project_name,
                worktree_path=self.project_path,
            )
            logger.info("Updated SESSION_SUMMARY.md")
        except Exception as exc:
            logger.warning("Failed to write SESSION_SUMMARY.md: %s", exc)

    def _auto_resurrect_failed(self, job: Any) -> None:
        """Requeue failed same-type jobs for this project.

        Event-driven recovery: ``job`` (extract_entities or generate_summary)
        just completed successfully, which is proof the backend is healthy
        again — so any jobs of the same type that previously exhausted
        their retries for the same project get another chance. Bounded by
        JobQueue.auto_requeue_failed's limit and requeue_count cap so a
        flapping backend can't loop forever; no polling or health checks.

        The whole thing — including resolving the project — is inside one
        try/except: ``job`` already completed successfully, so any fault
        here (including a fault in ``_resolve_project_id`` itself) must
        only be logged, never propagate up to ``process_one``'s outer
        try/except and reclassify the already-completed job as failed.
        """
        try:
            project_id = self._resolve_project_id(job)
            if not project_id:
                return
            requeued = self.queue.auto_requeue_failed(job.type, project_id)
            if requeued:
                logger.info(
                    "Auto-resurrected %d failed '%s' job(s) for project %s",
                    requeued, job.type, project_id[:8],
                )
        except Exception as exc:
            logger.warning(
                "Auto-resurrection failed for job %s: %s", job.id[:8], exc,
            )

    def _enqueue_staleness_check(self, job: Any) -> None:
        """Queue a staleness check after extraction if we can infer the project.

        ``job`` has already completed successfully, so — mirroring
        ``_auto_resurrect_failed`` — the whole thing including resolving the
        project is inside one try/except: any fault here (including a fault
        in ``_resolve_project_id`` itself) must only be logged, never
        propagate up to ``process_one``'s outer try/except and reclassify
        the already-completed job as failed.
        """
        try:
            project_id = self._resolve_project_id(job)
            if not project_id:
                return
            self.queue.enqueue(
                "staleness_check", {"project_id": project_id},
            )
        except Exception as exc:
            logger.warning("Failed to enqueue staleness_check: %s", exc)

    def _log_extraction_failure(self, job: Any, exc: Exception) -> None:
        """Append extraction failures to .callmem/extraction.log."""
        from datetime import datetime

        from callmem.compat import UTC

        log_path = self.db.db_path.parent / "extraction.log"
        ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
        line = f"[{ts}] Extraction job {job.id[:8]} failed: {exc}\n"
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(line)
        except OSError:
            pass

    def _resolve_project_id(self, job: Any) -> str | None:
        project_id = job.payload.get("project_id")
        if project_id:
            return str(project_id)
        session_id = job.payload.get("session_id")
        if not session_id:
            return None
        conn = self.db.connect()
        try:
            row = conn.execute(
                "SELECT project_id FROM sessions WHERE id = ?", (session_id,),
            ).fetchone()
            if row is None:
                return None
            return str(row["project_id"])
        finally:
            conn.close()

    def _run_loop(self) -> None:
        """Main polling loop — runs in background thread."""
        while self.running:
            try:
                processed = self.process_one()
                if not processed:
                    time.sleep(self.poll_interval)
            except Exception as exc:
                logger.error("Worker loop error: %s", exc)
                time.sleep(self.poll_interval)
