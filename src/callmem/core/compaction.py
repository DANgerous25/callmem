"""Memory compaction — age-based archival of old events and summaries.

Compaction NEVER deletes data — it sets archived_at timestamps.
Pinned entities and active TODOs are always protected.
Only events covered by a summary are archived.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from ulid import ULID

from callmem.compat import UTC

if TYPE_CHECKING:
    from callmem.core.database import Database
    from callmem.models.config import Config

logger = logging.getLogger(__name__)


@dataclass
class CompactionPolicy:
    raw_events_age_days: int = 1
    summaries_age_days: int = 7
    full_archive_age_days: int = 30
    max_db_size_mb: int = 500
    protect_pinned: bool = True
    protect_active_todos: bool = True


@dataclass
class CompactionStats:
    events_archived: int = 0
    summaries_archived: int = 0
    entities_archived: int = 0
    duration_ms: int = 0


class Compactor:
    """Archives old events and summaries based on age policies."""

    def __init__(self, db: Database, config: Config) -> None:
        self.db = db
        self.config = config
        self.policy = CompactionPolicy()

    def run(self, project_id: str) -> CompactionStats:
        """Run compaction for the given project."""
        start = time.monotonic()
        stats = CompactionStats()
        now = datetime.now(UTC).isoformat()

        effective_policy = self._effective_policy()
        conn = self.db.connect()
        try:
            stats.events_archived = self._archive_events(
                conn, project_id, effective_policy, now
            )
            stats.summaries_archived = self._archive_summaries(
                conn, project_id, effective_policy, now
            )
            stats.entities_archived = self._archive_entities(
                conn, project_id, effective_policy, now
            )
            conn.commit()
        finally:
            conn.close()

        elapsed = time.monotonic() - start
        stats.duration_ms = int(elapsed * 1000)

        self._log_run(
            project_id, stats, effective_policy, now
        )

        logger.info(
            "Compaction complete for %s: %d events, %d summaries, %d entities archived",
            project_id,
            stats.events_archived,
            stats.summaries_archived,
            stats.entities_archived,
        )

        return stats

    def _effective_policy(self) -> CompactionPolicy:
        policy = CompactionPolicy()
        db_size_mb = self._db_size_mb()
        if db_size_mb > policy.max_db_size_mb:
            policy.raw_events_age_days = max(1, policy.raw_events_age_days // 2)
            policy.summaries_age_days = max(1, policy.summaries_age_days // 2)
            policy.full_archive_age_days = max(1, policy.full_archive_age_days // 2)
        return policy

    def _db_size_mb(self) -> float:
        path = self.db.db_path
        if path == ":memory:":
            return 0.0
        try:
            return os.path.getsize(path) / (1024 * 1024)
        except OSError:
            return 0.0

    def _archive_events(
        self,
        conn: Any,
        project_id: str,
        policy: CompactionPolicy,
        now: str,
    ) -> int:
        threshold = (
            datetime.now(UTC) - timedelta(days=policy.raw_events_age_days)
        ).isoformat()

        result = conn.execute(
            "SELECT e.id FROM events e "
            "WHERE e.project_id = ? "
            "AND e.archived_at IS NULL "
            "AND e.timestamp < ? "
            "AND EXISTS ("
            "  SELECT 1 FROM summaries s "
            "  WHERE s.session_id = e.session_id "
            "  AND s.level IN ('chunk', 'session') "
            "  AND s.event_range_start <= e.timestamp "
            "  AND s.event_range_end >= e.timestamp "
            ")",
            (project_id, threshold),
        ).fetchall()

        protected_ids = self._protected_event_ids(conn, project_id)
        ids_to_archive = [
            r["id"] for r in result if r["id"] not in protected_ids
        ]

        if not ids_to_archive:
            return 0

        placeholders = ",".join("?" for _ in ids_to_archive)
        conn.execute(
            f"UPDATE events SET archived_at = ? "
            f"WHERE id IN ({placeholders})",
            (now, *ids_to_archive),
        )

        return len(ids_to_archive)

    def _archive_summaries(
        self,
        conn: Any,
        project_id: str,
        policy: CompactionPolicy,
        now: str,
    ) -> int:
        threshold = (
            datetime.now(UTC) - timedelta(days=policy.summaries_age_days)
        ).isoformat()

        result = conn.execute(
            "SELECT s.id FROM summaries s "
            "WHERE s.project_id = ? "
            "AND s.archived_at IS NULL "
            "AND s.level = 'chunk' "
            "AND s.created_at < ? "
            "AND EXISTS ("
            "  SELECT 1 FROM summaries s2 "
            "  WHERE s2.session_id = s.session_id "
            "  AND s2.level = 'session' "
            ")",
            (project_id, threshold),
        ).fetchall()

        ids = [r["id"] for r in result]
        if not ids:
            return 0

        placeholders = ",".join("?" for _ in ids)
        conn.execute(
            f"UPDATE summaries SET archived_at = ? "
            f"WHERE id IN ({placeholders})",
            (now, *ids),
        )

        return len(ids)

    def _archive_entities(
        self,
        conn: Any,
        project_id: str,
        policy: CompactionPolicy,
        now: str,
    ) -> int:
        threshold = (
            datetime.now(UTC) - timedelta(days=policy.full_archive_age_days)
        ).isoformat()

        clauses = [
            "project_id = ?",
            "archived_at IS NULL",
            "updated_at < ?",
        ]
        params: list[Any] = [project_id, threshold]

        if policy.protect_pinned:
            clauses.append("pinned = 0")
        if policy.protect_active_todos:
            clauses.append(
                "(type != 'todo' OR status != 'open')"
            )

        where = " AND ".join(clauses)
        result = conn.execute(
            f"SELECT id FROM entities WHERE {where}",
            params,
        ).fetchall()

        ids = [r["id"] for r in result]
        if not ids:
            return 0

        placeholders = ",".join("?" for _ in ids)
        conn.execute(
            f"UPDATE entities SET archived_at = ? "
            f"WHERE id IN ({placeholders})",
            (now, *ids),
        )

        return len(ids)

    def _protected_event_ids(
        self, conn: Any, project_id: str
    ) -> set[str]:
        protected: set[str] = set()

        if self.policy.protect_active_todos:
            rows = conn.execute(
                "SELECT DISTINCT source_event_id FROM entities "
                "WHERE project_id = ? AND type = 'todo' "
                "AND status = 'open' AND source_event_id IS NOT NULL",
                (project_id,),
            ).fetchall()
            protected.update(r["source_event_id"] for r in rows)

        if self.policy.protect_pinned:
            rows = conn.execute(
                "SELECT DISTINCT source_event_id FROM entities "
                "WHERE project_id = ? AND pinned = 1 "
                "AND source_event_id IS NOT NULL",
                (project_id,),
            ).fetchall()
            protected.update(r["source_event_id"] for r in rows)

        return protected

    def _log_run(
        self,
        project_id: str,
        stats: CompactionStats,
        policy: CompactionPolicy,
        now: str,
    ) -> None:
        conn = self.db.connect()
        try:
            conn.execute(
                "INSERT INTO compaction_log "
                "(id, project_id, run_at, events_archived, "
                "summaries_created, entities_merged, duration_ms, policy_config) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(ULID()),
                    project_id,
                    now,
                    stats.events_archived,
                    0,
                    stats.entities_archived,
                    stats.duration_ms,
                    json.dumps({
                        "raw_events_age_days": policy.raw_events_age_days,
                        "summaries_age_days": policy.summaries_age_days,
                        "full_archive_age_days": policy.full_archive_age_days,
                    }),
                ),
            )
            conn.commit()
        finally:
            conn.close()
