"""Entity extraction from raw events using Ollama.

Processes raw events and extracts structured entities
(decisions, TODOs, facts, failures, discoveries).
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from callmem.core.anchors import parse_file_anchors
from callmem.core.json_utils import parse_json
from callmem.core.prompts import EXTRACTION_PROMPT
from callmem.core.queue import JobQueue
from callmem.models.entities import Entity

if TYPE_CHECKING:
    from callmem.core.database import Database
    from callmem.core.embeddings import Embedder
    from callmem.core.ollama import OllamaClient
    from callmem.core.resolve_judge import ResolutionCandidate
    from callmem.models.config import Config

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
MAX_EVENTS_PER_JOB = 50


def _mentions_entity_id(text: str, entity_id: str) -> bool:
    """True if ``text`` quotes ``entity_id``'s short (last-8-char, with
    or without a leading '#') or full form, case-insensitively.

    This is the auto-resolve discussion guard's entire signal: genuine
    completion work almost never quotes the todo's own ID, while
    triage/review text nearly always does when discussing it. Cheap
    substring check — no LLM involved.
    """
    if not text or not entity_id:
        return False
    text_lower = text.lower()
    short_id = entity_id[-8:].lower()
    return (
        entity_id.lower() in text_lower
        or short_id in text_lower
        or f"#{short_id}" in text_lower
    )


class EntityExtractor:
    """Extracts structured entities from events using the local LLM."""

    def __init__(
        self,
        db: Database,
        ollama: OllamaClient,
        event_bus: Any | None = None,
        config: Config | None = None,
    ) -> None:
        self.db = db
        self.ollama = ollama
        self.queue = JobQueue(db)
        self.event_bus = event_bus
        # Optional: without a config the extractor cannot know whether
        # embeddings are wanted, so it queues none. The daemon's
        # WorkerRunner always passes one.
        self.config = config

    def enqueue_extraction(
        self, event_ids: list[str], session_id: str | None = None
    ) -> list[str]:
        """Queue extraction jobs for the given events.

        If the event count exceeds MAX_EVENTS_PER_JOB, the batch is split
        into multiple jobs to avoid exceeding the LLM context window.
        Returns the list of job IDs created.
        """
        max_events = MAX_EVENTS_PER_JOB
        if max_events <= 0 or len(event_ids) <= max_events:
            payload: dict[str, Any] = {"event_ids": event_ids}
            if session_id is not None:
                payload["session_id"] = session_id
            return [self.queue.enqueue("extract_entities", payload)]

        job_ids: list[str] = []
        for i in range(0, len(event_ids), max_events):
            chunk = event_ids[i:i + max_events]
            payload = {"event_ids": chunk}
            if session_id is not None:
                payload["session_id"] = session_id
            job_ids.append(self.queue.enqueue("extract_entities", payload))
            logger.info(
                "Split extraction job: %d events (batch %d/%d)",
                len(chunk), (i // max_events) + 1,
                (len(event_ids) + max_events - 1) // max_events,
            )
        return job_ids

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
                entities = self.process_job(job)
                all_entities.extend(entities)
                self.queue.complete(job.id)
            except Exception as exc:
                logger.error(
                    "Extraction job %s failed: %s", job.id, exc
                )
                self.queue.fail(job.id, str(exc))

        return all_entities

    def process_job(self, job: Any) -> list[Entity]:
        """Process a single already-claimed extraction job.

        Raises on failure. Does not touch the job's queue status — the
        caller (``process_pending`` above, or ``WorkerRunner.process_one``
        for a job it dequeued itself) owns that job's complete/fail.
        """
        event_ids = job.payload.get("event_ids", [])
        if not event_ids:
            return []

        events = self._fetch_events(event_ids)
        if not events:
            return []

        events_text = self._format_events(events)
        session_id = job.payload.get("session_id")
        prior_titles = self._fetch_prior_titles(session_id) if session_id else ""
        if not prior_titles:
            prior_titles = "(none yet — this is the first extraction in the session)"
        prompt = EXTRACTION_PROMPT.format(
            events_text=events_text,
            prior_titles=prior_titles,
        )
        response = self.ollama.extract(prompt)
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
                    source_event_ids=list(event_ids) if event_ids else None,
                    type=entity_type,
                    title=title,
                    content=content,
                    key_points=key_points,
                    synopsis=synopsis,
                    status=item.get("status"),
                    priority=item.get("priority"),
                    extracted_by=getattr(self.ollama, "model", None),
                )
                self._insert_entity(entity)
                entities.append(entity)
                self._insert_anchors_from_entity(entity)
                files = item.get("files", [])
                if isinstance(files, list) and files:
                    self._insert_entity_files(entity.id, files)
                if self.event_bus is not None:
                    self.event_bus.publish("entity_created", entity.to_row())

        if entities:
            self._auto_resolve(project_id, entities)
            self._enqueue_embeddings(project_id, entities)
            self._consolidate(project_id, entities)

        return entities

    def _consolidate(self, project_id: str, entities: list[Entity]) -> None:
        """Run LLM-routed consolidation against similar existing entities.

        Never raises: the entities are already persisted, so a
        consolidation fault must not fail an otherwise-successful
        extraction job (same discipline as ``_enqueue_embeddings``). Worst
        case, this batch's entities simply stay as-is until the next
        extraction run gets a chance to consolidate them.
        """
        if self.config is None or not self.config.consolidation.enabled:
            return
        try:
            from callmem.core.consolidation import EntityConsolidator

            EntityConsolidator(self.db, self.ollama, self.config).consolidate(
                project_id, entities,
            )
        except Exception as exc:
            logger.warning("Consolidation failed for batch: %s", exc)

    def _enqueue_embeddings(
        self, project_id: str, entities: list[Entity]
    ) -> None:
        """Queue an ``embed_entities`` job for the entities just created.

        Never raises: the entities are already persisted, so a queueing
        fault must not fail an otherwise-successful extraction job. The
        worst case is these entities stay FTS-only until the next
        ``callmem embed --backfill``.
        """
        if self.config is None:
            return
        try:
            from callmem.core.embeddings import enqueue_embeddings

            enqueue_embeddings(
                self.queue, self.config, [e.id for e in entities], project_id,
            )
        except Exception as exc:
            logger.warning("Failed to enqueue embedding job: %s", exc)

    _RESOLUTION_DRIVER_TYPES = frozenset({"bugfix", "feature", "change"})
    _RESOLVABLE_TYPES = frozenset({"todo", "failure"})
    _KEYWORD_STOPWORDS = frozenset({
        "implement", "update", "add", "fix", "create", "remove",
        "build", "write", "setup", "configure", "install", "test",
        "also", "with", "from", "that", "this", "which", "where",
    })

    def _auto_resolve(
        self, project_id: str, new_entities: list[Entity]
    ) -> int:
        """Auto-resolve open TODOs/failures matching newly-extracted drivers.

        Called at the end of each extraction job with the entities that
        were just created. Delegates keyword matching and resolution to
        ``_resolve_by_drivers``; ``sweep_resolutions`` uses the same
        helper to retroactively close items the live hook missed.
        """
        drivers = [
            (e.title, e.type, e.id) for e in new_entities
            if e.type in self._RESOLUTION_DRIVER_TYPES and e.title
        ]
        stats: dict[str, int] = {}
        count = self._resolve_by_drivers(project_id, drivers, stats=stats)
        if stats.get("skipped_by_guard"):
            logger.info(
                "Auto-resolve: skipped %d resolution(s) (discussion guard)",
                stats["skipped_by_guard"],
            )
        return count

    def sweep_resolutions(
        self,
        project_id: str,
        dry_run: bool = False,
        stats: dict[str, int] | None = None,
    ) -> list[dict[str, Any]]:
        """Retroactively auto-resolve TODOs/failures against prior drivers.

        The live auto-resolve hook only fires at extraction time, so
        any TODO created after its resolving feature was extracted
        never gets matched. This sweep walks every non-stale driver
        entity in the project against the current open pool. Returns
        a list of ``{id, type, title, status, driver_title}`` dicts
        describing what was (or would be) closed.

        If ``stats`` is given, it is populated with sweep counters —
        currently just ``skipped_by_guard``, the number of matches the
        discussion guard held back (see ``_resolve_by_drivers``) — so
        callers like the CLI's ``--dry-run`` output can report them.
        """
        conn = self.db.connect()
        try:
            rows = conn.execute(
                "SELECT id, type, title FROM entities "
                "WHERE project_id = ? AND type IN (?, ?, ?) "
                "AND stale = 0 AND title IS NOT NULL "
                "ORDER BY created_at ASC",
                (project_id, "bugfix", "feature", "change"),
            ).fetchall()
            drivers = [(r["title"], r["type"], r["id"]) for r in rows]
        finally:
            conn.close()

        return self._resolve_by_drivers(
            project_id, drivers, dry_run=dry_run, collect=True, stats=stats,
        )

    def gather_resolution_candidates(
        self,
        project_id: str,
        stats: dict[str, int] | None = None,
        embedder: Embedder | None = None,
    ) -> list[ResolutionCandidate]:
        """Recall stage for the JUDGED sweep (``callmem resolve``'s default
        mode): find candidate driver/target pairs but close NONE of them —
        ``ResolutionJudge`` decides that from evidence. Keyword matching is
        the base recall (via ``_resolve_by_drivers`` with ``auto_close=
        False``), optionally widened with embedding similarity when this
        project has vector data, so paraphrased matches keyword recall
        misses still get a chance at being judged. Falls back to
        keyword-only, unchanged, whenever vector data is unusable (feature
        off, none stored, backend unreachable) — same degradation
        discipline as ``consolidation.py``.

        Every driver's source text is fetched once, up front, in a single
        batched query (``Repository.get_entities_source_text``) and shared
        by both the keyword stage's discussion guard and the embedding
        stage's — the sweep runs over every driver in the project, so a
        per-driver query here would be the N+1 the live path can afford
        but a sweep cannot.
        """
        conn = self.db.connect()
        try:
            rows = conn.execute(
                "SELECT id, type, title FROM entities "
                "WHERE project_id = ? AND type IN (?, ?, ?) "
                "AND stale = 0 AND title IS NOT NULL "
                "ORDER BY created_at ASC",
                (project_id, "bugfix", "feature", "change"),
            ).fetchall()
            drivers = [(r["title"], r["type"], r["id"]) for r in rows]
        finally:
            conn.close()

        if not drivers:
            if stats is not None:
                stats["skipped_by_guard"] = 0
            return []

        from callmem.core.repository import Repository

        repo = Repository(self.db)
        driver_ids = [d[2] for d in drivers if d[2]]
        driver_source_map = repo.get_entities_source_text(driver_ids)

        pairs = self._resolve_by_drivers(
            project_id, drivers, auto_close=False, collect=True,
            stats=stats, driver_source_map=driver_source_map,
        )
        pairs = self._widen_recall_with_embeddings(
            project_id, drivers, pairs, driver_source_map,
            stats=stats, embedder=embedder,
        )
        if not pairs:
            return []

        from callmem.core.resolve_judge import ResolutionCandidate

        all_ids = sorted(
            {p["driver_id"] for p in pairs} | {p["id"] for p in pairs}
        )
        rows_by_id = {r["id"]: r for r in repo.get_entities_by_ids(all_ids)}

        candidates: list[ResolutionCandidate] = []
        for p in pairs:
            driver_row = rows_by_id.get(p["driver_id"])
            target_row = rows_by_id.get(p["id"])
            if driver_row is None or target_row is None:
                continue
            candidates.append(ResolutionCandidate(
                driver_id=p["driver_id"],
                driver_title=driver_row.get("title") or "",
                driver_content=driver_row.get("content") or "",
                driver_source_text=driver_source_map.get(p["driver_id"], ""),
                target_id=p["id"],
                target_type=p["type"],
                target_title=target_row.get("title") or "",
                target_content=target_row.get("content") or "",
            ))
        return candidates

    _EMBED_WIDEN_TOP_K = 3

    def _widen_recall_with_embeddings(
        self,
        project_id: str,
        drivers: list[tuple[str, str, str]],
        existing_pairs: list[dict[str, Any]],
        driver_source_map: dict[str, str],
        stats: dict[str, int] | None = None,
        embedder: Embedder | None = None,
    ) -> list[dict[str, Any]]:
        """Add extra (driver, target) candidate pairs found via embedding
        similarity — keyword recall only matches shared words, so a
        paraphrased completion never surfaces without this. Returns
        ``existing_pairs`` unchanged whenever vector data is unusable for
        this project (embeddings disabled, none stored, or the backend is
        unreachable/misconfigured) — same degradation discipline as
        ``consolidation.py``.
        """
        if self.config is None or not self.config.embeddings.enabled:
            return existing_pairs

        from callmem.core.repository import Repository

        repo = Repository(self.db)
        if not repo.has_embeddings(project_id):
            return existing_pairs

        if embedder is None:
            from callmem.core.embeddings import create_embedder

            embedder = create_embedder(self.config)
        if embedder is None:
            return existing_pairs

        from callmem.core.embeddings import (
            embedding_model_key,
            entity_embedding_text,
            rank_by_similarity,
        )

        settings = self.config.embeddings
        model_key = embedding_model_key(self.config)
        vector_candidates = repo.load_embedding_candidates(
            project_id, model_key, types=list(self._RESOLVABLE_TYPES),
            include_stale=False, limit=settings.candidate_limit,
        )
        if not vector_candidates:
            return existing_pairs

        open_targets = {
            r["id"]: r for r in repo.get_entities_by_ids(
                [c["entity_id"] for c in vector_candidates],
                include_stale=False,
            )
            if r.get("status") in ("open", "unresolved")
        }
        if not open_targets:
            return existing_pairs

        driver_by_id = {d[2]: d[0] for d in drivers if d[2]}
        driver_ids = list(driver_by_id.keys())
        texts = [
            settings.document_prefix + entity_embedding_text({
                "title": driver_by_id[did], "synopsis": None,
                "key_points": None, "content": None,
            })
            for did in driver_ids
        ]
        vectors = embedder.embed(texts, timeout=settings.timeout)
        if not vectors or len(vectors) != len(driver_ids):
            return existing_pairs

        claimed = {p["id"] for p in existing_pairs}
        skipped_by_guard = 0
        widened = list(existing_pairs)

        for driver_id, vector in zip(driver_ids, vectors, strict=True):
            if not vector:
                continue
            scored = rank_by_similarity(
                vector, vector_candidates,
                min_similarity=settings.min_similarity,
            )
            found = 0
            for _score, target_id in scored:
                if found >= self._EMBED_WIDEN_TOP_K:
                    break
                if target_id in claimed or target_id not in open_targets:
                    continue
                if _mentions_entity_id(
                    driver_source_map.get(driver_id, ""), target_id,
                ):
                    skipped_by_guard += 1
                    continue

                target_row = open_targets[target_id]
                resolved_status = (
                    "done" if target_row["type"] == "todo" else "resolved"
                )
                widened.append({
                    "id": target_id,
                    "type": target_row["type"],
                    "title": target_row["title"],
                    "status": resolved_status,
                    "driver_title": driver_by_id[driver_id],
                    "driver_id": driver_id,
                })
                claimed.add(target_id)
                found += 1

        if skipped_by_guard and stats is not None:
            stats["skipped_by_guard"] = (
                stats.get("skipped_by_guard", 0) + skipped_by_guard
            )

        return widened

    def _resolve_by_drivers(
        self,
        project_id: str,
        drivers: list[tuple[str, str, str]],
        dry_run: bool = False,
        collect: bool = False,
        stats: dict[str, int] | None = None,
        auto_close: bool = True,
        driver_source_map: dict[str, str] | None = None,
    ) -> Any:
        """Run resolution logic for a list of (title, type, id) driver triples.

        When ``collect`` is True, returns a list of resolution records
        suitable for CLI output; otherwise returns the count. When
        ``auto_close`` is False (the judged sweep's recall stage), matches
        are collected exactly like ``dry_run`` — never written to the DB,
        because closing is deferred to ``ResolutionJudge`` — while live
        auto-resolve and the legacy (``--no-judge``) sweep both leave this
        at its default of True.

        Discussion guard: before treating a candidate match as resolvable,
        checks whether the driver's own source text quotes the match's
        short or full ID — ``driver_source_map`` when the caller already
        batched it (the judged sweep), else fetched lazily once per driver
        (the live path, unchanged). Genuine completion work almost never
        quotes the todo's own ID; triage/review text nearly always does
        when discussing it — so a hit means "this driver is talking about
        the target, not resolving it," and the match is skipped rather
        than closed. That trade-off is deliberately one-sided: a
        stuck-open TODO from a false skip is visible and recoverable; a
        wrongly-closed one wasn't. ``stats["skipped_by_guard"]`` tallies
        how many matches this held back.
        """
        skipped_by_guard = 0
        if not drivers:
            if stats is not None:
                stats["skipped_by_guard"] = skipped_by_guard
            return [] if collect else 0

        from callmem.core.repository import Repository

        repo = Repository(self.db)
        open_statuses = ["open", "unresolved"]
        records: list[dict[str, Any]] = []
        closed_ids: set[str] = set()
        count = 0

        for title, _source_type, driver_id in drivers:
            words = [
                w for w in title.split()
                if len(w) > 3 and w.lower() not in self._KEYWORD_STOPWORDS
            ]
            if len(words) < 2:
                continue

            matches = repo.find_open_entities_by_keywords(
                project_id=project_id,
                entity_types=list(self._RESOLVABLE_TYPES),
                statuses=open_statuses,
                keywords=words,
                limit=3,
            )
            if not matches:
                continue

            # Lazy + shared across this driver's matches: same source
            # text answers the guard check for every candidate below.
            driver_source_text: str | None = None

            for match in matches:
                if match["id"] in closed_ids:
                    continue

                if driver_source_text is None:
                    if driver_source_map is not None:
                        driver_source_text = driver_source_map.get(
                            driver_id, "",
                        )
                    else:
                        driver_source_text = (
                            repo.get_entity_source_text(driver_id)
                            if driver_id else ""
                        )
                if _mentions_entity_id(driver_source_text, match["id"]):
                    # Biased toward skipping on purpose: a stuck-open TODO
                    # is visible and recoverable; a wrongly-closed one wasn't.
                    skipped_by_guard += 1
                    logger.debug(
                        "Auto-resolve guard: skipping %s '%s' -- driver "
                        "'%s' source text quotes the target's own ID "
                        "(likely discussion, not completion)",
                        match["type"], match["title"][:60], title[:60],
                    )
                    continue

                resolved_status = (
                    "done" if match["type"] == "todo" else "resolved"
                )
                record = {
                    "id": match["id"],
                    "type": match["type"],
                    "title": match["title"],
                    "status": resolved_status,
                    "driver_title": title,
                    "driver_id": driver_id,
                }
                if dry_run or not auto_close:
                    closed_ids.add(match["id"])
                    records.append(record)
                    continue
                result = repo.mark_resolved(match["id"], resolved_status)
                if result is not None and not result.get("unchanged"):
                    closed_ids.add(match["id"])
                    count += 1
                    records.append(record)
                    logger.info(
                        "Auto-resolved %s '%s' -> %s (matched by '%s')",
                        match["type"],
                        match["title"][:60],
                        resolved_status,
                        title[:60],
                    )

        if stats is not None:
            stats["skipped_by_guard"] = skipped_by_guard

        return records if collect else count

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

    def _fetch_prior_titles(self, session_id: str, limit: int = 40) -> str:
        """Return a newline-separated `- type: title` list of entities already
        extracted in this session, so the LLM can avoid emitting near-duplicates.

        Capped at ``limit`` recent titles to bound the prompt size — extraction
        jobs run roughly every ~20 events, so 40 prior titles is far more than
        a healthy session should ever produce.
        """
        conn = self.db.connect()
        try:
            rows = conn.execute(
                "SELECT type, title FROM entities "
                "WHERE source_event_id IN ("
                "  SELECT id FROM events WHERE session_id = ?"
                ") ORDER BY created_at DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        finally:
            conn.close()
        if not rows:
            return ""
        return "\n".join(f"- {r['type']}: {r['title']}" for r in rows)

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
                "(id, project_id, source_event_id, source_event_ids, type, "
                "title, content, "
                "key_points, synopsis, extracted_by, "
                "status, priority, pinned, created_at, updated_at, "
                "resolved_at, metadata, archived_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    row["id"], row["project_id"], row["source_event_id"],
                    row["source_event_ids"],
                    row["type"], row["title"], row["content"],
                    row["key_points"], row["synopsis"], row["extracted_by"],
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

    def _insert_anchors_from_entity(self, entity: Entity) -> None:
        """Parse deterministic file:line anchors out of entity content
        and persist them into entity_files.

        Extends the population path above (which stores the LLM's
        freeform "files" list) with anchors parsed directly from the
        entity's own text — precise enough (file + line) to validate
        against the working tree later. Both write into the same table;
        INSERT OR IGNORE means whichever runs first for a given path
        wins, so this is called before the freeform files insert to
        prefer the more precise anchor.
        """
        text = "\n".join(
            part for part in (
                entity.title, entity.content, entity.key_points,
                entity.synopsis,
            )
            if part
        )
        anchors = parse_file_anchors(text)
        if not anchors:
            return
        from callmem.core.repository import Repository

        Repository(self.db).insert_entity_file_anchors(entity.id, anchors)
