"""LLM-routed consolidation of newly-extracted entities.

Runs once per extraction batch (see ``EntityExtractor.process_job``): for
every entity just created, look up its top-K most similar existing
non-archived entities of the same type (vector search when embeddings are
available, FTS text-similarity fallback otherwise -- same degradation
discipline as ``retrieval.py``'s hybrid search). Entities whose best match
clears the configured threshold go into ONE batched LLM judgment call;
anything below the threshold is left alone (ADD, no LLM call spent on it).

Batch siblings can never be candidates for each other. Entities are
persisted (and FTS-indexed) before consolidation runs, so without an
explicit exclusion a same-batch entity would look like a legitimate
"existing" candidate -- a judge mistakenly returning mutual NOOP would
archive both copies of the same new fact, and mutual UPDATE would create a
supersede 2-cycle. Every candidate lookup excludes the whole batch's ids,
not just the entity being judged, and ``_parse`` rejects any
``existing_id`` that names a batch entity as a second line of defense.

Verdicts are applied through the existing non-destructive staleness verbs
-- nothing is ever deleted:
  - ADD:         the default. No action.
  - UPDATE:      the new entity refines an existing one. The existing entity
                 is marked stale + superseded_by=new (reason "consolidated");
                 the new entity is kept as-is. The existing entity's
                 cited_count/last_cited_at transfer onto the new entity --
                 it is the one that will be retrieved going forward, and
                 briefing importance ranking must not forget the credit
                 the old entity earned.
  - NOOP:        the new entity duplicates an existing one with nothing new.
                 The new entity is archived immediately; the existing
                 entity's updated_at is bumped so it surfaces as current. If
                 that existing entity was itself superseded elsewhere in the
                 same batch, the touch (and the "duplicate of") redirects to
                 its still-active successor instead -- the survivor must be
                 live. This redirect is resolved only after every
                 UPDATE/CONTRADICTS decision in the batch has been applied,
                 so it does not depend on which order entities were judged
                 or listed in. The archived entity's cited_count/
                 last_cited_at transfer onto whichever entity survives.
  - CONTRADICTS: the new entity conflicts with an existing one rather than
                 refining it. BOTH entities are kept, but the existing
                 entity is marked stale + superseded_by=new (reason
                 "contradicted") and additionally stamped with
                 invalidated_at, so `callmem stale` can list it distinctly
                 from ordinary superseded/manual staleness. Uses the same
                 checked-no-op guard as every other staleness verb: if the
                 existing entity is already stale (e.g. another entity in
                 this same batch already contradicted or updated it), the
                 mark is skipped and NOT double-counted.

Fail-open is the hard requirement here: any judge response that isn't
exactly the expected JSON shape -- covering every candidate entity with a
valid verdict and, for UPDATE/NOOP, a string id drawn from that entity's
own candidate list -- is treated as a bad LLM day. Every entity in the
batch stays ADD, the failure is logged loudly, and the run is still
recorded in ``consolidation_log`` with ``judge_failed=True``.

Ships disabled by default (see ``ConsolidationConfig``): the similarity
threshold is uncalibrated and this feature archives user memory, so it is
opt-in per project until tuned against real data.
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

_VALID_VERDICTS = {"ADD", "UPDATE", "NOOP", "CONTRADICTS"}
_MAX_CONTENT_CHARS = 300

# What the judge is asked to return per new entity: (verdict, existing_id).
_Decision = tuple[str, "str | None"]


@dataclass
class ConsolidationStats:
    """Per-run counters, persisted to ``consolidation_log``."""

    added: int = 0
    updated: int = 0
    noop: int = 0
    contradicted: int = 0
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
        otherwise, and no entity in ``entities`` can ever be a candidate
        for another entity also in ``entities``.
        """
        settings = self.config.consolidation
        if not settings.enabled or not entities or self.llm is None:
            return ConsolidationStats()

        batch_ids = {e.id for e in entities}
        # One embed() call for the whole batch rather than one per entity.
        vectors_by_id = self._embed_batch(project_id, entities) or {}

        qualifying: dict[str, tuple[Entity, list[dict[str, Any]]]] = {}
        for entity in entities:
            hits = self._find_similar(
                project_id, entity, settings.top_k, batch_ids,
                vectors_by_id.get(entity.id),
            )
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
        decisions = self._parse(raw, qualifying, batch_ids)

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
        logger.info(
            "Consolidation run for project %s: %d added, %d updated, "
            "%d noop, %d contradicted",
            project_id, stats.added, stats.updated, stats.noop,
            stats.contradicted,
        )
        self._log_run(project_id, stats)
        return stats

    # -- similarity lookup ------------------------------------------------

    def _get_embedder(self) -> Embedder | None:
        if not self._embedder_resolved:
            self._embedder = create_embedder(self.config)
            self._embedder_resolved = True
        return self._embedder

    def _embed_batch(
        self, project_id: str, entities: list[Entity],
    ) -> dict[str, list[float]] | None:
        """Embed every entity in the batch with ONE backend call.

        Returns None when vector data is unusable for this project at
        all (feature off, no stored embeddings, backend unreachable, or
        the batched call itself failed) -- callers then fall back to FTS
        for every entity. A per-entity vector can still be missing from
        an otherwise-successful result (an empty vector for one input);
        that entity individually falls back to FTS.
        """
        settings = self.config.embeddings
        if not settings.enabled or not self.repo.has_embeddings(project_id):
            return None

        embedder = self._get_embedder()
        if embedder is None:
            return None

        texts = [
            settings.document_prefix + entity_embedding_text({
                "title": e.title, "synopsis": e.synopsis,
                "key_points": e.key_points, "content": e.content,
            })
            for e in entities
        ]
        vectors = embedder.embed(texts, timeout=settings.timeout)
        if not vectors or len(vectors) != len(entities):
            return None

        return {
            e.id: v for e, v in zip(entities, vectors, strict=True) if v
        }

    def _find_similar(
        self,
        project_id: str,
        entity: Entity,
        top_k: int,
        exclude_ids: set[str],
        vector: list[float] | None,
    ) -> list[dict[str, Any]]:
        """Top-K similar existing entities, most similar first.

        Vector search when a precomputed vector is available for this
        entity, FTS text-similarity otherwise. Both paths exclude every
        id in ``exclude_ids`` (the whole current batch, not just
        ``entity`` itself), archived entities, and stale entities, and
        restrict candidates to ``entity.type`` -- a `todo` should never
        be judged against a `fact`.
        """
        if vector is not None:
            hits = self._vector_candidates(
                project_id, entity, top_k, exclude_ids, vector,
            )
            if hits is not None:
                return hits
        return self._fts_similar(project_id, entity, top_k, exclude_ids)

    def _vector_candidates(
        self,
        project_id: str,
        entity: Entity,
        top_k: int,
        exclude_ids: set[str],
        vector: list[float],
    ) -> list[dict[str, Any]] | None:
        """Vector-ranked candidates for a precomputed ``vector``, or None
        when vector data is unusable for this candidate set -- signals
        the caller to fall back to FTS."""
        settings = self.config.embeddings
        model_key = embedding_model_key(self.config)
        candidates = self.repo.load_embedding_candidates(
            project_id, model_key, types=[entity.type],
            include_stale=False, limit=settings.candidate_limit,
        )
        if not candidates:
            return None

        # No floor here: an empty result at floor 0.0 can only mean "no
        # usable vector data for this candidate set" (e.g. every stored
        # vector has a different dimensionality than the query -- a
        # model mismatch). That is the fallback signal, so it must
        # return None, not []. The configured threshold is applied
        # later, by the caller, against these raw similarity scores.
        scored = rank_by_similarity(vector, candidates, min_similarity=0.0)
        if not scored:
            return None

        ids = [eid for _score, eid in scored if eid not in exclude_ids][:top_k]
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
        self,
        project_id: str,
        entity: Entity,
        top_k: int,
        exclude_ids: set[str],
    ) -> list[dict[str, Any]]:
        if not entity.title:
            return []
        # Over-fetch by the exclusion set's size so filtering out every
        # batch sibling (worst case: they all rank above the real
        # candidates) still leaves up to top_k real results.
        rows = self.repo.search_entities_fts(
            project_id, entity.title, types=[entity.type],
            include_stale=False, limit=top_k + len(exclude_ids),
        )
        scored = [
            dict(row, _similarity=_text_similarity(
                entity.title, row.get("title") or "",
            ))
            for row in rows if row["id"] not in exclude_ids
        ]
        scored.sort(key=lambda r: r["_similarity"], reverse=True)
        return scored[:top_k]

    # -- judge call ---------------------------------------------------

    def _parse(
        self,
        raw: str | None,
        qualifying: dict[str, tuple[Entity, list[dict[str, Any]]]],
        batch_ids: set[str],
    ) -> dict[str, _Decision] | None:
        """Validate the judge's JSON, or None on any deviation from the
        expected shape -- the caller treats None as fail-open.

        Every id involved must be a string: a list/dict-valued id would
        raise TypeError on the ``in`` checks below (unhashable), which
        would bypass the fail-open path entirely -- so ids are type
        -checked before any membership test.
        """
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
            if not isinstance(new_id, str):
                return None
            if new_id not in qualifying or new_id in decisions:
                return None

            verdict = str(item.get("verdict", "")).strip().upper()
            if verdict not in _VALID_VERDICTS:
                return None

            existing_id = item.get("existing_id")
            if verdict in ("UPDATE", "NOOP", "CONTRADICTS"):
                if not isinstance(existing_id, str):
                    return None
                if existing_id in batch_ids:
                    # Belt-and-braces: candidate lookups already exclude
                    # every batch id (see _find_similar), so this should
                    # be unreachable. Treat it as malformed rather than
                    # ever risk a same-batch archive/supersede cycle.
                    return None
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
        """Plan every decision, then mutate in two ordered phases so the
        result never depends on ``entities`` iteration order.

        Phase 1 resolves every UPDATE/CONTRADICTS supersession. Phase 2
        resolves every NOOP redirect against the *complete* survivor map
        phase 1 produced. Splitting into phases (rather than interleaving,
        as a single pass over ``entities`` would) is what makes the NOOP
        redirect order-independent: a NOOP appearing before its target's
        UPDATE in ``entities`` now sees exactly the same survivor map as
        one appearing after, because phase 1 always finishes first
        regardless of where either decision sits in the batch.

        Within phase 1, two decisions naming the same ``existing_id`` (two
        UPDATEs, two CONTRADICTS, or one of each) are resolved
        deterministically by their position in ``entities``: each call
        into ``mark_stale`` is checked via its return value, so only the
        first one to actually flip the row (its ``WHERE stale = 0`` guard)
        counts as a supersession and enters the survivor map. Every
        subsequent claim on that same target finds it already stale,
        changes nothing, and falls back to ADD -- ``stats`` and the
        survivor map end up reflecting exactly what the DB recorded, never
        more.

        Citation transfer (``Repository.transfer_citations``) is gated on
        the same ``acted``/archived return values as the survivor map and
        stats above: it only runs the run that actually flips the row in
        the DB, and it is itself idempotent (the source's cited_count is
        zeroed as part of the same statement), so a duplicate claim or a
        re-run over already-processed entities can never double-count a
        citation. UPDATE transfers old -> new (the new entity is kept and
        inherits the credit); NOOP transfers new -> survivor (the new
        entity is the one being archived). CONTRADICTS does not transfer:
        the old entity is kept as a distinct, conflicting fact rather than
        being subsumed by the new one, so its citation credit is its own.
        """
        stats = ConsolidationStats()
        # (entity, existing_id) for every NOOP, deferred to phase 2 below.
        noop_pending: list[tuple[Entity, str]] = []
        # existing_id -> new_id, but only for supersessions the DB
        # actually applied this run (see mark_stale's return-value checks
        # below) -- an overwritten/no-op claim never enters this map.
        survivors: dict[str, str] = {}

        # Phase 1: every UPDATE/CONTRADICTS decision, in `entities` order.
        for entity in entities:
            decision = decisions.get(entity.id)
            if decision is None:
                stats.added += 1
                continue
            verdict, existing_id = decision
            if verdict == "NOOP" and existing_id is not None:
                noop_pending.append((entity, existing_id))
            elif verdict == "UPDATE" and existing_id is not None:
                acted = self.repo.mark_stale(
                    existing_id, reason="consolidated",
                    superseded_by=entity.id,
                )
                if acted:
                    survivors[existing_id] = entity.id
                    stats.updated += 1
                    # The old entity's citations belong to its
                    # replacement -- only on the run that actually
                    # superseded it, never on a checked no-op.
                    self.repo.transfer_citations(existing_id, entity.id)
                else:
                    # Checked no-op: another decision in this same batch
                    # already claimed this existing_id (duplicate
                    # existing_id across two UPDATE/CONTRADICTS
                    # decisions). Nothing changed in the DB, so this new
                    # entity is only ADDed, never counted as an update.
                    stats.added += 1
            elif verdict == "CONTRADICTS" and existing_id is not None:
                acted = self.repo.mark_stale(
                    existing_id, reason="contradicted",
                    superseded_by=entity.id, invalidated=True,
                )
                if acted:
                    survivors[existing_id] = entity.id
                    stats.contradicted += 1
                else:
                    # Checked no-op: the target was already stale (e.g. a
                    # sibling in this same batch already contradicted or
                    # updated it moments ago). Nothing was invalidated, so
                    # don't count a contradiction that didn't happen -- the
                    # new entity is still kept, same disposition as ADD.
                    stats.added += 1
            else:
                # ADD, or an UPDATE/NOOP/CONTRADICTS somehow missing its
                # existing_id -- _parse already guarantees the latter can't
                # happen, but falling back to ADD here costs nothing and
                # never deletes.
                stats.added += 1

        # Phase 2: every NOOP, redirected against the now-final survivor
        # map. A NOOP's existing_id names a pre-existing entity (never
        # another batch entity -- _parse rejects that), so at most one
        # hop through `survivors` is ever possible.
        for entity, existing_id in noop_pending:
            survivor_id = survivors.get(existing_id, existing_id)
            archived = self.repo.archive_entity(entity.id)
            self.repo.touch_entity(survivor_id)
            if archived:
                # The archived duplicate's citation credit belongs to the
                # survivor -- gated on `archived` so a re-run over an
                # entity this same id already archived (its `WHERE
                # archived_at IS NULL` guard) never transfers twice.
                self.repo.transfer_citations(entity.id, survivor_id)
            stats.noop += 1

        return stats

    def _log_run(self, project_id: str, stats: ConsolidationStats) -> None:
        self.repo.log_consolidation_run(
            project_id, added=stats.added, updated=stats.updated,
            noop=stats.noop, contradicted=stats.contradicted,
            judge_failed=stats.judge_failed,
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
