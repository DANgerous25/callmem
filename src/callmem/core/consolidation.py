"""LLM-routed consolidation of newly-extracted entities.

Runs once per extraction batch (see ``EntityExtractor.process_job``): for
every entity just created, look up its top-K most similar existing
non-archived entities of the same type (vector search when embeddings are
available, FTS text-similarity fallback otherwise -- same degradation
discipline as ``retrieval.py``'s hybrid search). Entities whose best match
clears the configured threshold go into ONE batched LLM judgment call;
anything below the threshold is left alone (ADD, no LLM call spent on it).

Verdicts are applied through the existing non-destructive staleness verbs
-- nothing is ever deleted:
  - ADD:    the default. No action.
  - UPDATE: the new entity refines an existing one. The existing entity is
            marked stale + superseded_by=new (reason "consolidated"); the
            new entity is kept as-is.
  - NOOP:   the new entity duplicates an existing one with nothing new.
            The new entity is archived immediately; the existing entity's
            updated_at is bumped so it surfaces as current.

Fail-open is the hard requirement here: any judge response that isn't
exactly the expected JSON shape -- covering every candidate entity with a
valid verdict and, for UPDATE/NOOP, an id drawn from that entity's own
candidate list -- is treated as a bad LLM day. Every entity in the batch
stays ADD, the failure is logged loudly, and the run is still recorded in
``consolidation_log`` with ``judge_failed=True``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import TYPE_CHECKING, Any

from callmem.core.embeddings import (
    create_embedder,
    embedding_model_key,
    entity_embedding_text,
    rank_by_similarity,
)
from callmem.core.json_utils import parse_json
from callmem.core.prompts import CONSOLIDATION_PROMPT
from callmem.core.repository import Repository

if TYPE_CHECKING:
    from callmem.core.database import Database
    from callmem.core.embeddings import Embedder
    from callmem.models.config import Config
    from callmem.models.entities import Entity

logger = logging.getLogger(__name__)

_VALID_VERDICTS = {"ADD", "UPDATE", "NOOP"}
_MAX_CONTENT_CHARS = 300

# What the judge is asked to return per new entity: (verdict, existing_id).
_Decision = tuple[str, "str | None"]


@dataclass
class ConsolidationStats:
    """Per-run counters, persisted to ``consolidation_log``."""

    added: int = 0
    updated: int = 0
    noop: int = 0
    judge_failed: bool = False


class EntityConsolidator:
    """Judges a freshly-extracted entity batch against similar existing ones."""

    def __init__(
        self,
        db: Database,
        llm: Any,
        config: Config,
        embedder: Embedder | None = None,
    ) -> None:
        self.db = db
        self.repo = Repository(db)
        self.llm = llm
        self.config = config
        self._embedder = embedder
        self._embedder_resolved = embedder is not None

    def consolidate(
        self, project_id: str, entities: list[Entity],
    ) -> ConsolidationStats:
        """Run one consolidation pass over a just-created entity batch.

        Only entities passed in ``entities`` are ever touched as the
        "new" side; candidates are read-only until a verdict says
        otherwise.
        """
        settings = self.config.consolidation
        if not settings.enabled or not entities or self.llm is None:
            return ConsolidationStats()

        qualifying: dict[str, tuple[Entity, list[dict[str, Any]]]] = {}
        for entity in entities:
            hits = self._find_similar(project_id, entity, settings.top_k)
            if hits and hits[0]["_similarity"] >= settings.threshold:
                qualifying[entity.id] = (entity, hits)

        if not qualifying:
            stats = ConsolidationStats(added=len(entities))
            self._log_run(project_id, stats)
            return stats

        prompt = CONSOLIDATION_PROMPT.format(
            entries_block=_format_entries(qualifying.values()),
        )
        raw = self.llm.extract(prompt)
        decisions = self._parse(raw, qualifying)

        if decisions is None:
            logger.warning(
                "Consolidation judge returned malformed or absent output "
                "for %d candidate entit%s -- keeping all %d entities from "
                "this batch as ADD (fail-open). Raw response: %r",
                len(qualifying), "y" if len(qualifying) == 1 else "ies",
                len(entities), (raw or "")[:200],
            )
            stats = ConsolidationStats(added=len(entities), judge_failed=True)
            self._log_run(project_id, stats)
            return stats

        stats = self._apply(entities, decisions)
        self._log_run(project_id, stats)
        return stats

    # -- similarity lookup ------------------------------------------------

    def _get_embedder(self) -> Embedder | None:
        if not self._embedder_resolved:
            self._embedder = create_embedder(self.config)
            self._embedder_resolved = True
        return self._embedder

    def _find_similar(
        self, project_id: str, entity: Entity, top_k: int,
    ) -> list[dict[str, Any]]:
        """Top-K similar existing entities, most similar first.

        Vector search when this project has usable embeddings, FTS
        text-similarity otherwise. Both paths exclude ``entity`` itself,
        archived entities, and stale entities, and restrict candidates to
        ``entity.type`` -- a `todo` should never be judged against a
        `fact`.
        """
        vector_hits = self._vector_similar(project_id, entity, top_k)
        if vector_hits is not None:
            return vector_hits
        return self._fts_similar(project_id, entity, top_k)

    def _vector_similar(
        self, project_id: str, entity: Entity, top_k: int,
    ) -> list[dict[str, Any]] | None:
        """Vector-ranked candidates, or None when vector data is unusable
        for this project -- signals the caller to fall back to FTS."""
        settings = self.config.embeddings
        if not settings.enabled or not self.repo.has_embeddings(project_id):
            return None

        embedder = self._get_embedder()
        if embedder is None:
            return None

        text = settings.document_prefix + entity_embedding_text({
            "title": entity.title, "synopsis": entity.synopsis,
            "key_points": entity.key_points, "content": entity.content,
        })
        vectors = embedder.embed([text], timeout=settings.timeout)
        if not vectors or not vectors[0]:
            return None

        model_key = embedding_model_key(self.config)
        candidates = self.repo.load_embedding_candidates(
            project_id, model_key, types=[entity.type],
            include_stale=False, limit=settings.candidate_limit,
        )
        if not candidates:
            return None

        # No floor here: an empty result at floor 0.0 can only mean "no
        # usable vector data for this candidate set", which is exactly
        # the fallback signal. The configured threshold is applied later,
        # by the caller, against these raw similarity scores.
        scored = rank_by_similarity(vectors[0], candidates, min_similarity=0.0)
        ids = [eid for _score, eid in scored if eid != entity.id][:top_k]
        if not ids:
            return []

        scores = {eid: score for score, eid in scored}
        rows = {
            row["id"]: row
            for row in self.repo.get_entities_by_ids(
                ids, types=[entity.type], include_stale=False,
            )
        }
        return [
            dict(rows[eid], _similarity=scores[eid])
            for eid in ids if eid in rows
        ]

    def _fts_similar(
        self, project_id: str, entity: Entity, top_k: int,
    ) -> list[dict[str, Any]]:
        if not entity.title:
            return []
        rows = self.repo.search_entities_fts(
            project_id, entity.title, types=[entity.type],
            include_stale=False, limit=top_k + 1,
        )
        scored = [
            dict(row, _similarity=_text_similarity(
                entity.title, row.get("title") or "",
            ))
            for row in rows if row["id"] != entity.id
        ]
        scored.sort(key=lambda r: r["_similarity"], reverse=True)
        return scored[:top_k]

    # -- judge call ---------------------------------------------------

    def _parse(
        self,
        raw: str | None,
        qualifying: dict[str, tuple[Entity, list[dict[str, Any]]]],
    ) -> dict[str, _Decision] | None:
        """Validate the judge's JSON, or None on any deviation from the
        expected shape -- the caller treats None as fail-open."""
        if not raw:
            return None
        try:
            data = parse_json(raw)
        except json.JSONDecodeError:
            return None
        if not isinstance(data, list) or not data:
            return None

        decisions: dict[str, _Decision] = {}
        for item in data:
            if not isinstance(item, dict):
                return None
            new_id = item.get("new_id")
            if new_id not in qualifying or new_id in decisions:
                return None
            verdict = str(item.get("verdict", "")).strip().upper()
            if verdict not in _VALID_VERDICTS:
                return None
            existing_id = item.get("existing_id")
            if verdict in ("UPDATE", "NOOP"):
                candidate_ids = {c["id"] for c in qualifying[new_id][1]}
                if existing_id not in candidate_ids:
                    return None
            else:
                existing_id = None
            decisions[new_id] = (verdict, existing_id)

        # A partial response -- some qualifying entity never judged -- is
        # exactly as dangerous as a malformed one: fail open on the whole
        # batch rather than guess.
        if set(decisions) != set(qualifying):
            return None
        return decisions

    def _apply(
        self, entities: list[Entity], decisions: dict[str, _Decision],
    ) -> ConsolidationStats:
        stats = ConsolidationStats()
        for entity in entities:
            decision = decisions.get(entity.id)
            if decision is None:
                stats.added += 1
                continue
            verdict, existing_id = decision
            if verdict == "UPDATE" and existing_id is not None:
                self.repo.mark_stale(
                    existing_id, reason="consolidated",
                    superseded_by=entity.id,
                )
                stats.updated += 1
            elif verdict == "NOOP" and existing_id is not None:
                self.repo.archive_entity(entity.id)
                self.repo.touch_entity(existing_id)
                stats.noop += 1
            else:
                # ADD, or an UPDATE/NOOP somehow missing its existing_id --
                # _parse already guarantees the latter can't happen, but
                # falling back to ADD here costs nothing and never deletes.
                stats.added += 1
        return stats

    def _log_run(self, project_id: str, stats: ConsolidationStats) -> None:
        self.repo.log_consolidation_run(
            project_id, added=stats.added, updated=stats.updated,
            noop=stats.noop, judge_failed=stats.judge_failed,
        )


def _text_similarity(a: str, b: str) -> float:
    """Cheap [0,1] title-similarity used as the FTS-fallback ranking
    signal -- candidates are already FTS-prefiltered, so a title-only
    ratio is enough to rank and threshold them."""
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def _truncate(value: str | None, limit: int) -> str:
    value = value or ""
    return value if len(value) <= limit else value[:limit] + "..."


def _format_entries(
    items: Any,
) -> str:
    parts = []
    for idx, (entity, candidates) in enumerate(items, start=1):
        cand_lines = "\n".join(
            f"     [{c['id']}] {c['title']!r} -- "
            f"{_truncate(c.get('content'), _MAX_CONTENT_CHARS)!r}"
            for c in candidates
        )
        parts.append(
            f"{idx}. NEW ENTRY id={entity.id} type={entity.type}\n"
            f"   title: {entity.title!r}\n"
            f"   content: {_truncate(entity.content, _MAX_CONTENT_CHARS)!r}\n"
            f"   Candidates:\n{cand_lines}"
        )
    return "\n\n".join(parts)
