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
                 last_cited_at transfer onto whichever entity survives, and
                 its own superseded_by is set to that survivor too, so a
                 citation landing on it after this run still finds the
                 live entity to credit instead of stranding on a dead row.
  - CONTRADICTS: the new entity conflicts with an existing one rather than
                 refining it. BOTH entities are kept, but the existing
                 entity is marked stale + superseded_by=new (reason
                 "contradicted") and additionally stamped with
                 invalidated_at, so `callmem stale` can list it distinctly
                 from ordinary superseded/manual staleness. Uses the same
                 checked-no-op guard as every other staleness verb: if the
                 existing entity is already stale (e.g. another entity in
                 this same batch already contradicted or updated it), the
                 mark is skipped and NOT double-counted. Its cited_count/
                 last_cited_at transfer onto the new entity too: `stale`
                 excludes it from briefings either way, and cited_count is
                 a topical-attention signal, not a correctness signal, so
                 a correction to a heavily-cited fact must not start cold.

A collision between two decisions naming the same ``existing_id`` (two
UPDATEs, an UPDATE and a CONTRADICTS, etc.) is resolved by position in
``entities`` -- first claim wins, everything after it degrades to ADD
-- and logged as a warning, since it means the judge response itself is
incoherent (two new entities disputing one memory). A NOOP's survivor is
also re-verified live immediately before its duplicate is archived: if a
concurrent process (outside this batch) staled or archived it between the
judge call and here, the archive is skipped (both entities left
untouched, counted as ADD) rather than stranding the fact with no live
copy left.

Fail-open is the hard requirement here: any judge response that isn't
exactly the expected JSON shape -- covering every candidate entity with a
valid verdict and, for UPDATE/NOOP, a string id drawn from that entity's
own candidate list -- is treated as a bad LLM day. Every entity in the
batch stays ADD, the failure is logged loudly, and the run is still
recorded in ``consolidation_log`` with ``judge_failed=True``.

Ships disabled by default (see ``ConsolidationConfig``): the similarity
threshold is uncalibrated and this feature archives user memory, so it is
opt-in per project until tuned against real data.

``consolidate(..., dry_run=True)`` (see ``callmem consolidate --dry-run``
in cli.py) runs this exact same candidate-selection and judging path --
``_find_similar``, the batched judge call, ``_parse`` -- over a batch of
already-persisted entities and reports what it WOULD do, mutating
nothing: ``_apply`` takes an ``apply`` flag that swaps every write
(``mark_stale``/``archive_entity``/``touch_entity``/``transfer_citations``)
for the equivalent read (or, where the entity's own pre-run state already
answers the question, nothing at all), while walking the identical
plan-then-mutate control flow described above. This is deliberate: the
calibration harness is only trustworthy if it is measuring the real
decision logic, not a second reimplementation of it.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import TYPE_CHECKING, Any

import httpx

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

# What the judge is asked to return per new entity:
# (verdict, existing_id, reason).
_Decision = tuple[str, "str | None", "str | None"]


@dataclass
class ConsolidationDecision:
    """One entity's outcome, for the ``dry_run=True`` calibration report.

    Only populated on ``consolidate(..., dry_run=True)`` -- the live path
    has no use for it and leaves ``ConsolidationStats.decisions`` as
    None. ``verdict``/``existing_id`` are exactly what the judge
    returned for entities that reached it. ``reason`` is either the
    judge's own one-sentence rationale (verdict came from the judge),
    or one of two sentinels distinguishing WHY an entity never got a
    real judge-authored verdict: ``"below_threshold"`` (never sent --
    its best match never cleared the gate) or ``"judge_failed"`` (sent,
    but the whole batch's judge response was malformed/absent, so this
    entity fails open to ADD too). This three-way split is the single
    most decision-relevant one for calibration: "judged ADD just above
    threshold" and "never asked" look identical if conflated.

    Two similarity scores, not one, because they can legitimately
    differ: ``gate_similarity`` is always the top candidate's score --
    what the configured threshold actually compares against to decide
    whether this entity reaches the judge at all. ``matched_similarity``
    is the score of the SPECIFIC candidate named by ``existing_id``
    (None for ADD, which names no candidate) -- the judge is free to
    pick any candidate from the top-K list handed to it, not
    necessarily the highest-scoring one, so the two can diverge.
    Deriving a threshold from only one of these would be unsound.
    """

    entity_id: str
    entity_title: str
    verdict: str
    existing_id: str | None
    existing_title: str | None
    gate_similarity: float | None
    matched_similarity: float | None
    reason: str | None


@dataclass
class ConsolidationStats:
    """Per-run counters, persisted to ``consolidation_log``.

    ``transfer_attempts`` is in-memory only (not persisted): the number
    of times ``_apply`` called ``Repository.transfer_citations`` and the
    target row matched (``UPDATE ... WHERE id = ?`` found the row), a
    diagnostic for calibration, not a DB-backed count. This counts
    *attempts*, not credit actually moved: ``transfer_citations`` reports
    a row as modified whenever the target exists, even when the source
    had zero citations to give -- so this is closer to
    ``updated + contradicted + noop`` than to a citation-movement metric.
    Named for what it verifiably measures rather than what it might
    suggest.

    ``decisions`` is likewise in-memory only, and only ever populated by
    a ``dry_run=True`` call -- see ``ConsolidationDecision``.
    """

    added: int = 0
    updated: int = 0
    noop: int = 0
    contradicted: int = 0
    judge_failed: bool = False
    transfer_attempts: int = 0
    decisions: list[ConsolidationDecision] | None = None


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
        self, project_id: str, entities: list[Entity], dry_run: bool = False,
    ) -> ConsolidationStats:
        """Run one consolidation pass over a just-created entity batch.

        Only entities passed in ``entities`` are ever touched as the
        "new" side; candidates are read-only until a verdict says
        otherwise, and no entity in ``entities`` can ever be a candidate
        for another entity also in ``entities``.

        ``dry_run=True`` (see the module docstring, and ``callmem
        consolidate --dry-run`` in cli.py) runs every step below
        completely unchanged -- candidate lookup, the threshold gate,
        the batched judge call, ``_parse`` -- so a calibration run is
        judged by the exact same logic a live run would use. Only two
        things differ: the ``settings.enabled`` gate is bypassed (a
        project doing shadow-mode calibration has consolidation off by
        definition), and ``_apply`` is called with ``apply=False`` so it
        classifies every decision without writing to the DB.
        ``stats.decisions`` is populated with a per-entity report
        (similarity + judge reason) that only a dry run has any use for.
        """
        settings = self.config.consolidation
        if not entities or self.llm is None:
            return ConsolidationStats()
        if not dry_run and not settings.enabled:
            return ConsolidationStats()

        batch_ids = {e.id for e in entities}
        # One embed() call for the whole batch rather than one per entity.
        vectors_by_id = self._embed_batch(project_id, entities) or {}

        qualifying: dict[str, tuple[Entity, list[dict[str, Any]]]] = {}
        # Only collected when dry_run: (entity, hits) for entities whose
        # best match never cleared the threshold, so the report can show
        # their near-miss similarity too -- useful for picking a
        # threshold. The live path has no use for this, so it isn't
        # built there.
        below_threshold: list[tuple[Entity, list[dict[str, Any]]]] = []
        for entity in entities:
            hits = self._find_similar(
                project_id, entity, settings.top_k, batch_ids,
                vectors_by_id.get(entity.id),
            )
            if hits and hits[0]["_similarity"] >= settings.threshold:
                qualifying[entity.id] = (entity, hits)
            elif dry_run:
                below_threshold.append((entity, hits))

        if not qualifying:
            stats = ConsolidationStats(added=len(entities))
            if dry_run:
                stats.decisions = self._decision_report(
                    entities, qualifying, below_threshold, None,
                )
            else:
                self._log_run(project_id, stats)
            return stats

        prompt = CONSOLIDATION_PROMPT.format(
            entries_block=_format_entries(qualifying.values()),
        )
        try:
            raw = self.llm.extract(prompt)
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            # Both shipped clients (OllamaClient, OpenAICompatClient)
            # already catch httpx's connect/timeout/status errors
            # internally and return None -- this is the one gap: a
            # response body that isn't valid JSON (e.g. a proxy or
            # gateway hiccup returning an HTML error page with a 200
            # status). Treat it exactly like an absent response: `raw =
            # None` already routes through the same fail-open path
            # below, so a single transient judge call can never abort
            # the whole batch (or, via the CLI's per-entity sweep, the
            # rest of a large-corpus calibration run).
            logger.warning(
                "Consolidation judge call raised %s for %d candidate "
                "entit%s -- treating as a failed judge call (fail-open). "
                "%s",
                type(exc).__name__, len(qualifying),
                "y" if len(qualifying) == 1 else "ies", exc,
            )
            raw = None
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
            if dry_run:
                stats.decisions = self._decision_report(
                    entities, qualifying, below_threshold, None,
                    fallback_reason="judge_failed",
                )
            else:
                self._log_run(project_id, stats)
            return stats

        stats = self._apply(entities, decisions, apply=not dry_run)
        logger.info(
            "Consolidation %srun for project %s: %d added, %d updated, "
            "%d noop, %d contradicted",
            "dry-" if dry_run else "", project_id, stats.added,
            stats.updated, stats.noop, stats.contradicted,
        )
        if dry_run:
            stats.decisions = self._decision_report(
                entities, qualifying, below_threshold, decisions,
            )
        else:
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

            reason = item.get("reason")
            if not isinstance(reason, str):
                # Cosmetic-only: the judge's one-sentence rationale is
                # not part of the decision's validity, only its
                # calibration report line, so a missing/malformed reason
                # degrades to None rather than failing the whole batch.
                reason = None
            decisions[new_id] = (verdict, existing_id, reason)

        # A partial response -- some qualifying entity never judged -- is
        # exactly as dangerous as a malformed one: fail open on the whole
        # batch rather than guess.
        if set(decisions) != set(qualifying):
            return None
        return decisions

    def _apply(
        self,
        entities: list[Entity],
        decisions: dict[str, _Decision],
        apply: bool = True,
    ) -> ConsolidationStats:
        """Plan every decision, then mutate in two ordered phases so the
        NOOP redirect never depends on ``entities`` iteration order.

        ``apply=False`` (Task 4's dry-run) walks this exact same
        plan-then-mutate control flow -- same phase split, same
        collision resolution, same live-survivor recheck -- but swaps
        every write for the read (or already-known fact) that answers
        the same question the write's return value would have:
        ``mark_stale``'s ``WHERE stale = 0`` guard becomes
        ``is_entity_live``, and ``archive_entity``'s ``WHERE archived_at
        IS NULL`` guard becomes the entity's own ``archived_at`` as it
        stood when this batch was loaded (nothing in a dry run ever
        writes, so that value cannot go stale mid-run the way a
        concurrent process could still change it). ``touch_entity`` and
        ``transfer_citations`` are real mutations with no read-only
        equivalent, so they simply don't run -- ``stats.transfer_attempts``
        stays 0 on a dry run.

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
        changes nothing, and falls back to ADD -- ``stats`` end up
        reflecting exactly what the DB recorded, never more. What IS
        order-independent here is coarser than "the counts" as a whole:
        the *total* of ``added + updated + noop + contradicted`` (always
        ``len(entities)``) and the *number of supersessions* a colliding
        pair produces (always exactly one) hold regardless of order. The
        *individual* ``updated``/``contradicted`` split, and the
        *identity* of who wins, are NOT order-independent -- if UPDATE(a)
        and CONTRADICTS(b) both target the same existing_id, whichever
        comes first in ``entities`` decides both which entity the
        existing one ends up superseded by
        (superseded_by=a/reason="consolidated" vs.
        superseded_by=b/reason="contradicted"+invalidated_at) and which
        counter records the single supersession: order ``[a, b]`` yields
        ``stats.updated == 1, stats.contradicted == 0``; order ``[b, a]``
        yields the reverse. That is inherent to first-claim-wins
        tie-breaking, not a bug: the judge response itself is incoherent
        (two new entities disputing one memory), so there is no
        order-independent "correct" winner -- or "correct" counter -- to
        pick. What matters is that exactly one claim wins, ``stats``
        reflects what the DB actually recorded, and the collision is
        logged rather than silently decided (see ``_log_duplicate_claim``).
        A caller reordering or parallelising a batch for this reason alone
        will still see identical DB state and identical *totals*, just
        not necessarily the same updated/contradicted split.

        Citation transfer (``Repository.transfer_citations``) is gated on
        the same ``acted``/archived return values as the survivor map and
        stats above: it only runs on the run that actually flips the row
        in the DB, and it is itself idempotent (the source's cited_count
        is zeroed as part of the same statement), so a duplicate claim or
        a re-run over already-processed entities can never double-count a
        citation. UPDATE and CONTRADICTS both transfer old -> new (the
        existing entity is superseded either way -- refined or
        contradicted, its citation credit belongs to whichever entity
        replaces it in the visible corpus now that `stale` excludes it
        from briefings regardless of which verdict retired it; cited_count
        is a topical-attention signal, not a correctness signal, so a
        correction to a heavily-cited fact must not start cold). NOOP
        transfers new -> survivor (the new entity is the one being
        archived).

        Phase 2 also re-verifies each NOOP's survivor is still live
        (neither archived nor stale) immediately before archiving its
        duplicate: everything upstream of ``_apply`` -- candidate lookup,
        the judge call -- ran earlier, so a concurrent process outside
        this batch could have staled or archived the survivor in the
        meantime. Archiving the duplicate against a dead survivor would
        remove the fact from the visible corpus entirely, so that case
        degrades to ADD instead (both entities left untouched) and is
        logged. ``is_entity_live`` and the ``archive_entity`` call that
        follows it are two separate connections/statements, not one
        transaction, so this only narrows the TOCTOU window between the
        check and the archive -- it is not a lock, and a change landing
        in that narrower window is still possible in principle.
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
            verdict, existing_id, _reason = decision
            if verdict == "NOOP" and existing_id is not None:
                noop_pending.append((entity, existing_id))
            elif verdict == "UPDATE" and existing_id is not None:
                if apply:
                    acted = self.repo.mark_stale(
                        existing_id, reason="consolidated",
                        superseded_by=entity.id,
                    )
                else:
                    # Same question mark_stale's guard answers (is the
                    # target still live?), without writing -- and
                    # `existing_id not in survivors` reproduces "did an
                    # earlier decision THIS run already claim it" the
                    # same way the real guard would (that claim already
                    # flipped stale=1 by the time this one runs).
                    acted = (
                        existing_id not in survivors
                        and self.repo.is_entity_live(existing_id)
                    )
                if acted:
                    survivors[existing_id] = entity.id
                    stats.updated += 1
                    # The old entity's citations belong to its
                    # replacement -- only on the run that actually
                    # superseded it, never on a checked no-op.
                    if apply and self.repo.transfer_citations(existing_id, entity.id):
                        stats.transfer_attempts += 1
                else:
                    # Checked no-op: another decision in this same batch
                    # already claimed this existing_id (duplicate
                    # existing_id across two UPDATE/CONTRADICTS
                    # decisions). Nothing changed in the DB, so this new
                    # entity is only ADDed, never counted as an update.
                    self._log_duplicate_claim(entity, existing_id, verdict, survivors)
                    stats.added += 1
            elif verdict == "CONTRADICTS" and existing_id is not None:
                if apply:
                    acted = self.repo.mark_stale(
                        existing_id, reason="contradicted",
                        superseded_by=entity.id, invalidated=True,
                    )
                else:
                    acted = (
                        existing_id not in survivors
                        and self.repo.is_entity_live(existing_id)
                    )
                if acted:
                    survivors[existing_id] = entity.id
                    stats.contradicted += 1
                    # See the phase-1 docstring note above: `stale`
                    # excludes the contradicted entity from briefings
                    # regardless, so its citation credit transfers to
                    # its replacement the same as UPDATE's.
                    if apply and self.repo.transfer_citations(existing_id, entity.id):
                        stats.transfer_attempts += 1
                else:
                    # Checked no-op: the target was already stale (e.g. a
                    # sibling in this same batch already contradicted or
                    # updated it moments ago). Nothing was invalidated, so
                    # don't count a contradiction that didn't happen -- the
                    # new entity is still kept, same disposition as ADD.
                    self._log_duplicate_claim(entity, existing_id, verdict, survivors)
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
            if not self.repo.is_entity_live(survivor_id):
                # The survivor went stale/archived after the judge call --
                # nothing in THIS batch can do that to a NOOP's survivor
                # (phase 1 already ran; a survivor redirected via the map
                # is by construction the entity phase 1 just marked as
                # the successor, and an un-redirected survivor was never
                # touched by this run at all), so this can only be an
                # external, concurrent change. Archiving the duplicate
                # now would strand the fact with no live copy left.
                logger.warning(
                    "Consolidation NOOP: %s named survivor %s, which is "
                    "no longer live (archived or stale) at apply time -- "
                    "skipping the archive to avoid losing the fact from "
                    "the visible corpus; treating as ADD instead.",
                    entity.id, survivor_id,
                )
                stats.added += 1
                continue

            # archive_entity's guard only cares whether archived_at was
            # already set -- and nothing in a dry run writes, so the
            # entity's own pre-run state still answers that question
            # correctly (see the class-level docstring). superseded_by is
            # recorded on the archived duplicate too (survivor_id, the
            # same entity touch_entity below bumps), not just its own
            # citation transfer: a citation that lands on this row AFTER
            # this run -- the post-hoc case _resolve_live_citation_target
            # exists for -- has to find that link to avoid stranding the
            # credit on a dead row.
            archived = (
                self.repo.archive_entity(entity.id, superseded_by=survivor_id)
                if apply
                else entity.archived_at is None
            )
            if archived:
                if apply:
                    self.repo.touch_entity(survivor_id)
                    # The archived duplicate's citation credit belongs to
                    # the survivor -- gated on `archived` so a re-run
                    # over an entity this same id already archived (its
                    # `WHERE archived_at IS NULL` guard) never transfers
                    # twice.
                    if self.repo.transfer_citations(entity.id, survivor_id):
                        stats.transfer_attempts += 1
                stats.noop += 1
            else:
                # Checked no-op: entity.id was already archived before
                # this run reached it -- an external race today (every
                # entity here started this run with archived_at NULL, so
                # something else archived it in the meantime), and more
                # directly reachable once Task 4's dry-run starts judging
                # pre-existing entities. Nothing changed in the DB, so
                # it's not a fresh NOOP.
                stats.added += 1

        return stats

    def _decision_report(
        self,
        entities: list[Entity],
        qualifying: dict[str, tuple[Entity, list[dict[str, Any]]]],
        below_threshold: list[tuple[Entity, list[dict[str, Any]]]],
        decisions: dict[str, _Decision] | None,
        fallback_reason: str | None = None,
    ) -> list[ConsolidationDecision]:
        """Build the calibration report row for every entity in this
        dry-run batch, purely by reading the same ``qualifying``/
        ``below_threshold``/``decisions`` structures ``consolidate``
        already produced -- no candidate lookup or judging happens here.

        ``decisions`` is None for the two fail-open exits (nothing
        cleared the threshold, or the judge response was malformed):
        every entity reports verdict "ADD" with similarity/candidate
        info still shown when it exists. ``reason`` disambiguates WHY:
        an entity that reached the judge (is in ``qualifying``) is
        tagged with ``fallback_reason`` (e.g. "judge_failed"); an entity
        that never reached it at all (its best match never cleared the
        threshold) is tagged "below_threshold" -- conflating these two
        into one untagged "ADD" bucket would hide the single most
        decision-relevant split calibration needs. Never a would-archive
        either way.
        """
        below_hits = {e.id: hits for e, hits in below_threshold}
        rows = []
        for entity in entities:
            _, q_hits = qualifying.get(entity.id, (None, None))
            hits = q_hits if q_hits is not None else below_hits.get(entity.id, [])

            verdict, existing_id, reason = "ADD", None, None
            if decisions is not None and entity.id in decisions:
                verdict, existing_id, reason = decisions[entity.id]
            elif entity.id in qualifying:
                # Reached the judge, but the batch's response as a whole
                # was malformed/absent -- fails open with a specific
                # reason, not silence.
                reason = fallback_reason
            else:
                # Never sent: this entity's own best match never cleared
                # settings.threshold.
                reason = "below_threshold"

            existing_title = None
            # The gate value: hits[0] is always the top-ranked candidate
            # (both _vector_candidates and _fts_similar return
            # highest-similarity first), i.e. exactly what
            # `settings.threshold` was compared against to decide
            # whether this entity even reached the judge.
            gate_similarity = hits[0]["_similarity"] if hits else None
            # The judge is free to name any candidate from the top-K
            # list, not necessarily hits[0] -- so the matched pair's own
            # score can differ from the gate value and must be reported
            # separately, not silently substituted for it.
            matched_similarity = None
            if existing_id is not None:
                match = next((h for h in hits if h["id"] == existing_id), None)
                if match is not None:
                    existing_title = match.get("title")
                    matched_similarity = match["_similarity"]

            rows.append(ConsolidationDecision(
                entity_id=entity.id, entity_title=entity.title,
                verdict=verdict, existing_id=existing_id,
                existing_title=existing_title,
                gate_similarity=gate_similarity,
                matched_similarity=matched_similarity,
                reason=reason,
            ))
        return rows

    def _log_duplicate_claim(
        self,
        entity: Entity,
        existing_id: str,
        verdict: str,
        survivors: dict[str, str],
    ) -> None:
        """An UPDATE/CONTRADICTS claim found its target already stale --
        logged so an incoherent judge response (two new entities naming
        the same existing_id) doesn't vanish without a trace. This is
        exactly the signal Task 4's calibration harness needs to surface,
        and today it's the only record: ``consolidation_log`` has no
        per-collision column.
        """
        winner = survivors.get(existing_id)
        logger.warning(
            "Consolidation: %s's %s claim on existing_id %s lost -- the "
            "target was already stale when this claim was applied (%s). "
            "%s stays ADD, not counted as %s.",
            entity.id, verdict, existing_id,
            f"already superseded by {winner} earlier in this batch"
            if winner is not None
            else "superseded outside this batch, or a race",
            entity.id, verdict.lower(),
        )

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
