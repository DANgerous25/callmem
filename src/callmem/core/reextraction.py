"""Re-extraction engine — re-process events through the extraction pipeline.

Allows users to upgrade entity quality after switching models without
re-importing sessions. Preserves user edits (pinned/modified entities)
and archives old entities instead of deleting them.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from callmem.core.extraction import EXTRACTION_BATCH_SIZE, EntityExtractor
from callmem.core.prompts import EXTRACTION_PROMPT
from callmem.models.entities import Entity

if TYPE_CHECKING:
    from callmem.core.database import Database
    from callmem.core.ollama import OllamaClient
    from callmem.models.config import Config

from callmem.compat import UTC

logger = logging.getLogger(__name__)


class ReExtractor:
    """Re-extracts entities from existing events using the current model."""

    def __init__(
        self,
        db: Database,
        ollama: OllamaClient,
        config: Config,
    ) -> None:
        self.db = db
        self.ollama = ollama
        self.config = config

    def count_events(
        self,
        project_id: str,
        session_id: str | None = None,
        since: str | None = None,
    ) -> int:
        """Count events matching the given filters."""
        conn = self.db.connect()
        try:
            clauses: list[str] = ["project_id = ?"]
            params: list[Any] = [project_id]

            if session_id is not None:
                clauses.append("session_id = ?")
                params.append(session_id)

            if since is not None:
                clauses.append("timestamp >= ?")
                params.append(since)

            where = " AND ".join(clauses)
            row = conn.execute(
                f"SELECT COUNT(*) as c FROM events WHERE {where}", params
            ).fetchone()
            return row["c"]
        finally:
            conn.close()

    def count_sessions(
        self,
        project_id: str,
        session_id: str | None = None,
        since: str | None = None,
    ) -> int:
        """Count sessions matching the given filters."""
        conn = self.db.connect()
        try:
            clauses: list[str] = ["project_id = ?"]
            params: list[Any] = [project_id]

            if session_id is not None:
                clauses.append("id = ?")
                params.append(session_id)

            if since is not None:
                clauses.append("started_at >= ?")
                params.append(since)

            where = " AND ".join(clauses)
            row = conn.execute(
                f"SELECT COUNT(*) as c FROM sessions WHERE {where}", params
            ).fetchone()
            return row["c"]
        finally:
            conn.close()

    def _parse_since(self, since: str) -> str | None:
        """Parse a --since value like '7d' into a datetime string."""
        if not since:
            return None
        stripped = since.strip()
        lower = stripped.lower()
        if lower.endswith("d") and lower[:-1].isdigit():
            days = int(lower[:-1])
            cutoff = datetime.now(UTC) - timedelta(days=days)
            return cutoff.isoformat()
        if lower.endswith("h") and lower[:-1].isdigit():
            hours = int(lower[:-1])
            cutoff = datetime.now(UTC) - timedelta(hours=hours)
            return cutoff.isoformat()
        return stripped

    def _get_event_batches(
        self,
        project_id: str,
        batch_size: int,
        session_id: str | None = None,
        since: str | None = None,
    ) -> list[list[dict[str, Any]]]:
        """Fetch events grouped into batches by session."""
        conn = self.db.connect()
        try:
            clauses: list[str] = ["project_id = ?"]
            params: list[Any] = [project_id]

            if session_id is not None:
                clauses.append("session_id = ?")
                params.append(session_id)

            if since is not None:
                clauses.append("timestamp >= ?")
                params.append(since)

            where = " AND ".join(clauses)
            rows = conn.execute(
                f"SELECT id, session_id, project_id, type, content, timestamp "
                f"FROM events WHERE {where} "
                f"ORDER BY session_id, timestamp ASC",
                params,
            ).fetchall()

            events = [dict(r) for r in rows]
        finally:
            conn.close()

        if not events:
            return []

        batches: list[list[dict[str, Any]]] = []
        for i in range(0, len(events), batch_size):
            batches.append(events[i : i + batch_size])
        return batches

    def _extract_batch(
        self,
        events: list[dict[str, Any]],
        extractor: EntityExtractor | None = None,
    ) -> list[tuple[Entity, list[str]]]:
        """Run extraction on a batch of events.

        Returns the entities that would be created, each paired with its
        associated file list, WITHOUT persisting them. Callers must only
        insert (and archive what they replace) once extraction has
        succeeded — persisting here would let a batch's own new entities
        get swept up by the batch's own archive-by-source-event step.
        """
        if extractor is None:
            extractor = EntityExtractor(self.db, self.ollama)

        events_text = extractor._format_events(events)
        # Re-extraction operates on historical events with no notion of a
        # live session, so no prior-titles dedup hint is available — pass
        # an explicit "none" sentinel to satisfy the prompt placeholder.
        prompt = EXTRACTION_PROMPT.format(
            events_text=events_text,
            prior_titles="(none — running a historical re-extraction pass)",
        )
        response = self.ollama._generate(prompt)
        if not response:
            raise RuntimeError(
                "Ollama returned no response for re-extraction batch"
            )

        from callmem.core.extraction import ENTITY_TYPE_MAP

        extracted = extractor._parse_extraction(response)
        if not extracted:
            return []

        project_id = events[0]["project_id"]
        event_ids = [e["id"] for e in events]

        entities: list[tuple[Entity, list[str]]] = []
        for category, items in extracted.items():
            entity_type = ENTITY_TYPE_MAP.get(category)
            if entity_type is None:
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                title = item.get("title", "")
                content = item.get("content", "")
                if not title:
                    continue

                key_points_list = item.get("key_points", [])
                if isinstance(key_points_list, list) and key_points_list:
                    key_points = "\n".join(
                        f"\u2022 {p}" for p in key_points_list
                    )
                else:
                    key_points = None

                synopsis = item.get("synopsis")
                if synopsis and not isinstance(synopsis, str):
                    synopsis = None

                source_event_id = event_ids[0] if event_ids else None
                entity = Entity(
                    project_id=project_id,
                    source_event_id=source_event_id,
                    source_event_ids=list(event_ids) if event_ids else None,
                    type=entity_type,
                    title=title,
                    content=content,
                    key_points=key_points,
                    synopsis=synopsis,
                    status=item.get("status"),
                    priority=item.get("priority"),
                )
                files = item.get("files", [])
                clean_files = (
                    [f for f in files if f] if isinstance(files, list) else []
                )
                entities.append((entity, clean_files))

        return entities

    def run(
        self,
        project_id: str,
        session_id: str | None = None,
        since: str | None = None,
        batch_size: int | None = None,
        force: bool = False,
        dry_run: bool = False,
        progress_callback: Any | None = None,
    ) -> dict[str, Any]:
        """Run re-extraction on matching events.

        Returns a summary dict with counts of events processed and entities created.
        """
        if batch_size is None:
            batch_size = self.config.extraction.batch_size or EXTRACTION_BATCH_SIZE

        since_dt = self._parse_since(since) if since else None

        total_events = self.count_events(project_id, session_id, since_dt)
        total_sessions = self.count_sessions(project_id, session_id, since_dt)

        if total_events == 0:
            return {
                "total_events": 0,
                "total_sessions": 0,
                "events_processed": 0,
                "entities_created": 0,
                "entities_archived": 0,
                "failed_batches": 0,
            }

        batches = self._get_event_batches(
            project_id, batch_size, session_id, since_dt
        )

        if dry_run:
            return {
                "total_events": total_events,
                "total_sessions": total_sessions,
                "batches": len(batches),
                "dry_run": True,
            }

        from callmem.core.repository import Repository

        repo = Repository(self.db)
        # Snapshot BEFORE the run starts: only entities that already
        # existed are archival candidates. Without this, a replacement
        # entity inserted mid-run would itself qualify for archival the
        # moment its own (already-covered) source events are checked.
        candidate_ids = repo.list_unarchived_entity_ids(project_id)

        events_processed = 0
        entities_created = 0
        entities_archived = 0
        failed_batches = 0
        # Event ids whose re-extraction has succeeded so far this run. An
        # old entity is only archived once ALL of its source events are in
        # this set — an entity originally extracted from a 5-event batch
        # may be split across several re-extraction batches when
        # batch_size differs, and archiving it as soon as the first of
        # those batches succeeds would silently lose the events covered
        # by a later batch that then fails.
        succeeded_event_ids: set[str] = set()
        extractor = EntityExtractor(self.db, self.ollama)

        for batch_idx, batch in enumerate(batches):
            event_ids = [e["id"] for e in batch]

            # Extract first. Only on success do we consider these events
            # covered and persist the replacements — a failed batch must
            # not leave a hole where old entities were archived but
            # nothing replaced them.
            try:
                new_entities = self._extract_batch(batch, extractor)
            except Exception as exc:
                logger.error(
                    "Re-extraction batch %d failed: %s", batch_idx, exc
                )
                failed_batches += 1
                events_processed += len(batch)
                if progress_callback is not None:
                    progress_callback({
                        "batch": batch_idx + 1,
                        "total_batches": len(batches),
                        "events_processed": events_processed,
                        "total_events": total_events,
                        "entities_created": entities_created,
                        "entities_archived": entities_archived,
                    })
                continue

            succeeded_event_ids.update(event_ids)
            archived = repo.archive_entities_with_full_coverage(
                project_id, succeeded_event_ids, candidate_ids, force=force,
            )
            entities_archived += archived

            for entity, files in new_entities:
                extractor._insert_entity(entity)
                extractor._insert_anchors_from_entity(entity)
                if files:
                    extractor._insert_entity_files(entity.id, files)
            entities_created += len(new_entities)
            events_processed += len(batch)

            if progress_callback is not None:
                progress_callback({
                    "batch": batch_idx + 1,
                    "total_batches": len(batches),
                    "events_processed": events_processed,
                    "total_events": total_events,
                    "entities_created": entities_created,
                    "entities_archived": entities_archived,
                })

        return {
            "total_events": total_events,
            "total_sessions": total_sessions,
            "events_processed": events_processed,
            "entities_created": entities_created,
            "entities_archived": entities_archived,
            "failed_batches": failed_batches,
        }
