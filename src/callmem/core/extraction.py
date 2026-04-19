"""Entity extraction from raw events using Ollama.

Processes raw events and extracts structured entities
(decisions, TODOs, facts, failures, discoveries).
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from callmem.core.json_utils import parse_json
from callmem.core.prompts import EXTRACTION_PROMPT
from callmem.core.queue import JobQueue
from callmem.models.entities import Entity

if TYPE_CHECKING:
    from callmem.core.database import Database
    from callmem.core.ollama import OllamaClient

logger = logging.getLogger(__name__)

ENTITY_TYPE_MAP = {
    "decisions": "decision",
    "todos": "todo",
    "facts": "fact",
    "failures": "failure",
    "discoveries": "discovery",
    "features": "feature",
    "bugfixes": "bugfix",
    "research": "research",
    "changes": "change",
}

EXTRACTION_BATCH_SIZE = 10


class EntityExtractor:
    """Extracts structured entities from events using the local LLM."""

    def __init__(
        self,
        db: Database,
        ollama: OllamaClient,
        event_bus: Any | None = None,
    ) -> None:
        self.db = db
        self.ollama = ollama
        self.queue = JobQueue(db)
        self.event_bus = event_bus

    def enqueue_extraction(
        self, event_ids: list[str], session_id: str | None = None
    ) -> str:
        """Queue an extraction job for the given events."""
        payload: dict[str, Any] = {"event_ids": event_ids}
        if session_id is not None:
            payload["session_id"] = session_id
        return self.queue.enqueue("extract_entities", payload)

    def process_pending(self) -> list[Entity]:
        """Process all pending extraction jobs.

        Returns all entities created across all processed jobs.
        """
        all_entities: list[Entity] = []

        while True:
            job = self.queue.dequeue("extract_entities")
            if job is None:
                break

            try:
                entities = self._process_job(job)
                all_entities.extend(entities)
                self.queue.complete(job.id)
            except Exception as exc:
                logger.error(
                    "Extraction job %s failed: %s", job.id, exc
                )
                self.queue.fail(job.id, str(exc))

        return all_entities

    def _process_job(self, job: Any) -> list[Entity]:
        """Process a single extraction job."""
        event_ids = job.payload.get("event_ids", [])
        if not event_ids:
            return []

        events = self._fetch_events(event_ids)
        if not events:
            return []

        events_text = self._format_events(events)
        prompt = EXTRACTION_PROMPT.format(events_text=events_text)
        response = self.ollama._generate(prompt)
        if response is None:
            raise RuntimeError("Ollama returned no response for extraction")

        extracted = self._parse_extraction(response)
        project_id = events[0]["project_id"]

        entities: list[Entity] = []
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
                    type=entity_type,
                    title=title,
                    content=content,
                    key_points=key_points,
                    synopsis=synopsis,
                    status=item.get("status"),
                    priority=item.get("priority"),
                )
                self._insert_entity(entity)
                entities.append(entity)
                files = item.get("files", [])
                if isinstance(files, list) and files:
                    self._insert_entity_files(entity.id, files)
                if self.event_bus is not None:
                    self.event_bus.publish("entity_created", entity.to_row())

        if entities:
            self._auto_resolve(project_id, entities)

        return entities

    def _auto_resolve(
        self, project_id: str, new_entities: list[Entity]
    ) -> int:
        """Auto-resolve open TODOs/failures that match new bugfixes/features/changes.

        When a new bugfix/feature/change is extracted, search for open TODOs
        or unresolved failures with similar titles and mark them as done/resolved.
        Returns the number of entities auto-resolved.
        """
        resolution_types = {"bugfix", "feature", "change"}
        resolvable_types = {"todo", "failure"}
        new_titles: list[tuple[str, str]] = []

        for e in new_entities:
            if e.type in resolution_types and e.title:
                new_titles.append((e.title, e.type))

        if not new_titles:
            return 0

        from callmem.core.repository import Repository

        repo = Repository(self.db)
        resolved = 0

        for title, _source_type in new_titles:
            words = [w for w in title.split() if len(w) > 3 and w.lower() not in {
                "implement", "update", "add", "fix", "create", "remove",
                "build", "write", "setup", "configure", "install", "test",
                "also", "with", "from", "that", "this", "which", "where",
            }]
            if len(words) < 2:
                continue

            open_statuses = ["open", "unresolved"]
            matches = repo.find_open_entities_by_keywords(
                project_id=project_id,
                entity_types=list(resolvable_types),
                statuses=open_statuses,
                keywords=words,
                limit=3,
            )

            for match in matches:
                resolved_status = (
                    "done" if match["type"] == "todo" else "resolved"
                )
                if repo.resolve_entity(match["id"], resolved_status):
                    resolved += 1
                    logger.info(
                        "Auto-resolved %s '%s' -> %s (matched by '%s')",
                        match["type"],
                        match["title"][:60],
                        resolved_status,
                        title[:60],
                    )

        return resolved

    def _fetch_events(
        self, event_ids: list[str]
    ) -> list[dict[str, Any]]:
        """Fetch events by their IDs."""
        if not event_ids:
            return []
        conn = self.db.connect()
        try:
            placeholders = ",".join("?" for _ in event_ids)
            rows = conn.execute(
                f"SELECT * FROM events WHERE id IN ({placeholders})",
                event_ids,
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def _format_events(self, events: list[dict[str, Any]]) -> str:
        """Format events into text for the extraction prompt."""
        parts: list[str] = []
        for ev in events:
            parts.append(
                f"[{ev.get('type', 'unknown')}] {ev.get('content', '')}"
            )
        return "\n".join(parts)

    def _parse_extraction(
        self, response: str
    ) -> dict[str, list[dict[str, str]]]:
        """Parse the LLM extraction response into categorized items."""
        try:
            raw = parse_json(response)
        except json.JSONDecodeError:
            logger.warning(
                "Extraction returned invalid JSON: %s", response[:200]
            )
            return {}

        if not isinstance(raw, dict):
            return {}

        result: dict[str, list[dict[str, str]]] = {}
        for key in (
            "decisions", "todos", "facts", "failures", "discoveries",
            "features", "bugfixes", "research", "changes",
        ):
            items = raw.get(key, [])
            if isinstance(items, list):
                result[key] = items

        return result

    def _insert_entity(self, entity: Entity) -> None:
        conn = self.db.connect()
        try:
            row = entity.to_row()
            conn.execute(
                "INSERT INTO entities "
                "(id, project_id, source_event_id, type, title, content, "
                "key_points, synopsis, "
                "status, priority, pinned, created_at, updated_at, "
                "resolved_at, metadata, archived_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    row["id"], row["project_id"], row["source_event_id"],
                    row["type"], row["title"], row["content"],
                    row["key_points"], row["synopsis"],
                    row["status"], row["priority"], row["pinned"],
                    row["created_at"], row["updated_at"],
                    row["resolved_at"], row["metadata"], row["archived_at"],
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def _insert_entity_files(
        self, entity_id: str, files: list[str]
    ) -> None:
        conn = self.db.connect()
        try:
            conn.executemany(
                "INSERT OR IGNORE INTO entity_files "
                "(entity_id, file_path, relation) VALUES (?, ?, 'related')",
                [(entity_id, f) for f in files if f],
            )
            conn.commit()
        finally:
            conn.close()
