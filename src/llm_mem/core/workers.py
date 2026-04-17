"""Background worker runner — processes the job queue.

Dispatches extraction, summarization, and compaction jobs.
Runs in a background thread alongside the MCP server.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING, Any

from llm_mem.core.compaction import Compactor
from llm_mem.core.extraction import EntityExtractor
from llm_mem.core.queue import JobQueue
from llm_mem.core.summarization import Summarizer

if TYPE_CHECKING:
    from llm_mem.core.database import Database
    from llm_mem.core.ollama import OllamaClient
    from llm_mem.models.config import Config

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
            "extract_entities": EntityExtractor(db, ollama, event_bus),
            "generate_summary": Summarizer(db, ollama),
            "compact": Compactor(db, config),
        }

    def start(self) -> None:
        """Start the worker loop in a background daemon thread."""
        self.running = True
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="llm-mem-worker"
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
            if job.type == "extract_entities":
                self._extractions_since_summary += 1
                if self._extractions_since_summary >= 5:
                    self._maybe_write_session_summary()
                    self._extractions_since_summary = 0
        except Exception as exc:
            logger.error("Job %s failed: %s", job.id[:8], exc)
            self.queue.fail(job.id, str(exc))

        return True

    def _dispatch(self, handler: Any, job: Any) -> None:
        """Dispatch a job to the appropriate handler method."""
        if isinstance(handler, (EntityExtractor, Summarizer)):
            handler.process_pending()
        elif isinstance(handler, Compactor):
            project_id = job.payload.get("project_id", "")
            handler.run(project_id)
        else:
            raise RuntimeError(f"No dispatch for handler: {type(handler)}")

    def _maybe_write_session_summary(self) -> None:
        """Write SESSION_SUMMARY.md if auto-write is enabled and project_path is set."""
        if not self.project_path:
            return
        if not self.config.briefing.auto_write_session_summary:
            return
        try:
            from llm_mem.core.briefing import BriefingGenerator
            from llm_mem.core.repository import Repository

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
