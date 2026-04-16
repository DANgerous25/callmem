"""Entity extraction from raw events using Ollama.

Processes raw events and extracts structured entities
(decisions, TODOs, facts, failures, discoveries).
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from llm_mem.core.prompts import EXTRACTION_PROMPT
from llm_mem.core.queue import JobQueue
from llm_mem.models.entities import Entity

if TYPE_CHECKING:
    from llm_mem.core.database import Database
    from llm_mem.core.ollama import OllamaClient

logger = logging.getLogger(__name__)

ENTITY_TYPE_MAP = {
    "decisions": "decision",
    "todos": "todo",
    "facts": "fact",
    "failures": "failure",
    "discoveries": "discovery",
}

EXTRACTION_BATCH_SIZE = 10


class EntityExtractor:
    """Extracts structured entities from events using the local LLM."""

    def __init__(self, db: Database, ollama: OllamaClient) -> None:
        self.db = db
        self.ollama = ollama
        self.queue = JobQueue(db)

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

                source_event_id = event_ids[0] if event_ids else None
                entity = Entity(
                    project_id=project_id,
                    source_event_id=source_event_id,
                    type=entity_type,
                    title=title,
                    content=content,
                    status=item.get("status"),
                    priority=item.get("priority"),
                )
                self._insert_entity(entity)
                entities.append(entity)

        return entities

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
            raw = json.loads(response)
        except json.JSONDecodeError:
            logger.warning(
                "Extraction returned invalid JSON: %s", response[:200]
            )
            return {}

        if not isinstance(raw, dict):
            return {}

        result: dict[str, list[dict[str, str]]] = {}
        for key in ("decisions", "todos", "facts", "failures", "discoveries"):
            items = raw.get(key, [])
            if isinstance(items, list):
                result[key] = items

        return result

    def _insert_entity(self, entity: Entity) -> None:
        """Insert an entity into the database."""
        conn = self.db.connect()
        try:
            row = entity.to_row()
            conn.execute(
                "INSERT INTO entities "
                "(id, project_id, source_event_id, type, title, content, "
                "status, priority, pinned, created_at, updated_at, "
                "resolved_at, metadata, archived_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    row["id"], row["project_id"], row["source_event_id"],
                    row["type"], row["title"], row["content"],
                    row["status"], row["priority"], row["pinned"],
                    row["created_at"], row["updated_at"],
                    row["resolved_at"], row["metadata"], row["archived_at"],
                ),
            )
            conn.commit()
        finally:
            conn.close()
