"""Multi-strategy retrieval engine.

Combines structured lookup, FTS5 search, and recency weighting
to find relevant memories.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from callmem.compat import UTC
from callmem.core.embeddings import (
    create_embedder,
    embedding_model_key,
    rank_by_similarity,
)

if TYPE_CHECKING:
    from callmem.core.embeddings import Embedder
    from callmem.core.repository import Repository
    from callmem.models.config import Config

RECENCY_HALF_LIFE_DAYS = 7.0
DEFAULT_STRATEGIES = ("fts", "entities")

#: Reciprocal rank fusion constant. 60 is the value from the original
#: Cormack et al. paper and the de-facto default across hybrid-search
#: implementations: large enough that the top few ranks of each list
#: stay close together, so a strong hit in one ranking is not buried by
#: a mediocre hit that happens to appear in both.
RRF_K = 60

#: Weight of the recency term in a fused score. Deliberately far smaller
#: than the gap between adjacent normalised RRF scores, so recency only
#: ever breaks ties rather than reordering genuinely different ranks.
RRF_RECENCY_TIEBREAK = 0.001

logger = logging.getLogger(__name__)

#: Degradation reasons already logged. Search runs on every agent turn, so
#: an unconditional warning would bury the log; but silent degradation is
#: exactly what makes "embeddings quietly stopped working" undebuggable.
#: One loud line per reason per process is the compromise.
_logged_degradations: set[str] = set()


def reset_degradation_log() -> None:
    """Forget which degradation reasons have been logged (tests)."""
    _logged_degradations.clear()


def _log_once(key: str, message: str, *args: Any) -> None:
    if key in _logged_degradations:
        return
    _logged_degradations.add(key)
    logger.warning(message, *args)


@dataclass
class SearchResult:
    """A single search result with scoring metadata."""

    id: str
    source_type: str  # event, entity, summary
    type: str         # prompt, decision, todo, etc.
    title: str | None
    content: str
    score: float
    timestamp: str
    session_id: str | None
    metadata: dict[str, Any] | None
    key_points: str | None = None
    synopsis: str | None = None
    extracted_by: str | None = None
    status: str | None = None
    priority: str | None = None
    pinned: bool = False
    stale: bool = False


def _recency_factor(timestamp: str, now: str | None = None) -> float:
    """Exponential decay: recent items score higher.

    Half-life is RECENCY_HALF_LIFE_DAYS days.
    """
    if not timestamp:
        return 1.0
    try:
        ts = datetime.fromisoformat(timestamp.replace(" ", "T"))
    except (ValueError, TypeError, AttributeError):
        return 1.0
    # SQLite's datetime('now') returns naive strings — treat naive as UTC
    # so SET-queries (mark_stale, set_pinned, mark_resolved) can coexist
    # with timezone-aware timestamps written by Python.
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    reference = datetime.fromisoformat(now) if now else datetime.now(UTC)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)
    age_days = max(0.0, (reference - ts).total_seconds() / 86400)
    return math.exp(-0.693 * age_days / RECENCY_HALF_LIFE_DAYS)


def _estimate_tokens(text: str) -> int:
    """Rough token estimation: ~4 chars per token."""
    return max(1, len(text) // 4)


class RetrievalEngine:
    """Multi-strategy retrieval over events, entities, and summaries."""

    def __init__(
        self,
        repo: Repository,
        config: Config,
        embedder: Embedder | None = None,
    ) -> None:
        self.repo = repo
        self.config = config
        self._embedder = embedder
        self._embedder_resolved = embedder is not None

    def search(
        self,
        project_id: str,
        query: str,
        types: list[str] | None = None,
        session_id: str | None = None,
        limit: int = 20,
        include_archived: bool = False,
        include_stale: bool = False,
        strategies: list[str] | None = None,
    ) -> list[SearchResult]:
        """Search across events and entities using multiple strategies."""
        results, _mode = self.search_with_mode(
            project_id, query, types=types, session_id=session_id,
            limit=limit, include_archived=include_archived,
            include_stale=include_stale, strategies=strategies,
        )
        return results

    def search_with_mode(
        self,
        project_id: str,
        query: str,
        types: list[str] | None = None,
        session_id: str | None = None,
        limit: int = 20,
        include_archived: bool = False,
        include_stale: bool = False,
        strategies: list[str] | None = None,
    ) -> tuple[list[SearchResult], str]:
        """Search, also reporting which entity ranking served the query.

        Mode is ``"hybrid"`` when FTS and vector rankings were fused, and
        ``"fts"`` whenever the vector side contributed nothing — no
        embeddings stored, feature disabled, or backend unreachable. In
        every ``"fts"`` case the results are identical to what this engine
        produced before embeddings existed.
        """
        active_strategies = strategies or list(DEFAULT_STRATEGIES)
        results: dict[str, SearchResult] = {}
        mode = "fts"

        if "fts" in active_strategies and query:
            self._search_fts(
                project_id, query, session_id, limit, results, types
            )

        if "entities" in active_strategies:
            mode = self._search_entities(
                project_id, query, types, session_id, limit, results,
                include_stale=include_stale,
            )

        ranked = sorted(results.values(), key=lambda r: r.score, reverse=True)

        if not include_archived:
            ranked = [r for r in ranked if r.metadata.get("archived_at") is None]

        return ranked[:limit], mode

    def get_recent(
        self,
        project_id: str,
        limit: int = 20,
        session_id: str | None = None,
    ) -> list[SearchResult]:
        """Get recent events ordered by timestamp, scored by recency."""
        events = self.repo.get_events(
            project_id, session_id=session_id, limit=limit
        )
        now = datetime.now(UTC).isoformat()
        results: list[SearchResult] = []
        for ev in events:
            recency = _recency_factor(ev.timestamp, now)
            results.append(SearchResult(
                id=ev.id,
                source_type="event",
                type=ev.type,
                title=None,
                content=ev.content,
                score=recency,
                timestamp=ev.timestamp,
                session_id=ev.session_id,
                metadata=ev.metadata,
            ))
        return results

    def _search_fts(
        self,
        project_id: str,
        query: str,
        session_id: str | None,
        limit: int,
        results: dict[str, SearchResult],
        types: list[str] | None,
    ) -> None:
        rows = self.repo.search_events_fts(project_id, query, limit)

        now = datetime.now(UTC).isoformat()
        for r in rows:
            if types and r["type"] not in types:
                continue
            recency = _recency_factor(r["timestamp"], now)
            score = 1.0 * recency
            results[r["id"]] = SearchResult(
                id=r["id"],
                source_type="event",
                type=r["type"],
                title=None,
                content=r["content"],
                score=score,
                timestamp=r["timestamp"],
                session_id=r["session_id"],
                metadata={"archived_at": r.get("archived_at")},
            )

    def _search_entities(
        self,
        project_id: str,
        query: str,
        types: list[str] | None,
        session_id: str | None,
        limit: int,
        results: dict[str, SearchResult],
        include_stale: bool = False,
    ) -> str:
        """Populate ``results`` with entity hits; returns the mode used."""
        relevance: dict[str, float] | None = None
        mode = "fts"

        if query:
            # Relevance search: entities_fts MATCH over ALL non-archived
            # entities, ranked by bm25 — no recency pre-limit, so an
            # older-but-relevant entity is never unreachable.
            rows = self.repo.search_entities_fts(
                project_id, query, types=types,
                include_stale=include_stale, limit=limit,
            )
            vector_ids = self._vector_ranked_ids(
                project_id, query, types, include_stale, limit,
            )
            if vector_ids:
                rows, relevance = self._fuse_rankings(
                    rows, vector_ids, types, include_stale, limit,
                )
                mode = "hybrid"
        else:
            # No query to rank by relevance — browse recent entities.
            rows = self.repo.list_entities_for_browse(
                project_id, types=types,
                include_stale=include_stale, limit=limit,
            )

        now = datetime.now(UTC).isoformat()

        for r in rows:
            recency = _recency_factor(r["updated_at"], now)
            pin_boost = 1.5 if r["pinned"] else 1.0
            stale_penalty = 0.3 if r["stale"] else 1.0
            if relevance is None:
                score = 0.8 * recency * pin_boost * stale_penalty
            else:
                # Fused ordering comes from RRF; recency only breaks ties.
                score = 0.8 * pin_boost * stale_penalty * (
                    relevance[r["id"]] + RRF_RECENCY_TIEBREAK * recency
                )

            results[r["id"]] = SearchResult(
                id=r["id"],
                source_type="entity",
                type=r["type"],
                title=r["title"],
                content=r["content"],
                score=score,
                timestamp=r["updated_at"],
                session_id=None,
                metadata={
                    "superseded_by": r["superseded_by"],
                    "staleness_reason": r["staleness_reason"],
                },
                key_points=r["key_points"],
                synopsis=r["synopsis"],
                extracted_by=r["extracted_by"],
                status=r["status"],
                priority=r["priority"],
                pinned=bool(r["pinned"]),
                stale=bool(r["stale"]),
            )

        return mode

    # ── Vector search & fusion ───────────────────────────────────────

    def _get_embedder(self) -> Embedder | None:
        """Resolve the configured embedder once, lazily.

        Only ever called after ``has_embeddings`` confirmed this project
        has vectors, so a project without embeddings never constructs a
        backend client at all.
        """
        if not self._embedder_resolved:
            self._embedder = create_embedder(self.config)
            self._embedder_resolved = True
        return self._embedder

    def _vector_ranked_ids(
        self,
        project_id: str,
        query: str,
        types: list[str] | None,
        include_stale: bool,
        limit: int,
    ) -> list[str]:
        """Entity IDs ranked by cosine similarity to ``query``.

        Returns an empty list for every degraded state — feature off, no
        vectors stored, backend down, model mismatch, nothing above the
        similarity floor — which is what makes the caller fall back to
        the untouched pure-FTS path.
        """
        settings = self.config.embeddings
        if not settings.enabled:
            return []
        if not self.repo.has_embeddings(project_id):
            return []

        embedder = self._get_embedder()
        if embedder is None:
            return []

        # Asymmetric task prefix: queries and documents are embedded
        # differently by design (see EmbeddingsConfig).
        #
        # query_timeout, not the ingest timeout: this call sits in the
        # interactive search path, and a hung backend must cost a search a
        # few seconds, not the full ingest budget.
        vectors = embedder.embed(
            [settings.query_prefix + query], timeout=settings.query_timeout,
        )
        if not vectors or not vectors[0]:
            _log_once(
                "query_embed_failed",
                "Query embedding failed or timed out (>%ss) — serving this "
                "search from FTS only. Further occurrences are not logged.",
                settings.query_timeout,
            )
            return []
        query_vector = vectors[0]

        model_key = embedding_model_key(self.config)
        candidates = self.repo.load_embedding_candidates(
            project_id, model_key, types=types,
            include_stale=include_stale, limit=settings.candidate_limit,
        )
        if not candidates:
            # This project HAS vectors (has_embeddings passed) but none for
            # the active model key — almost always a model or prefix change
            # that left the stored corpus behind. Silent FTS-only results
            # would look like the feature simply doing nothing.
            _log_once(
                f"no_candidates:{model_key}",
                "Project has embeddings but none for the active key %r — "
                "search is FTS-only until `callmem embed --backfill` "
                "re-embeds under the new model/prefix.",
                model_key,
            )
            return []

        scored = rank_by_similarity(
            query_vector, candidates, settings.min_similarity,
        )
        return [entity_id for _score, entity_id in scored[:limit]]

    def _fuse_rankings(
        self,
        fts_rows: list[dict[str, Any]],
        vector_ids: list[str],
        types: list[str] | None,
        include_stale: bool,
        limit: int,
    ) -> tuple[list[dict[str, Any]], dict[str, float]]:
        """Fuse the FTS and vector rankings with reciprocal rank fusion.

        Each list contributes ``1 / (RRF_K + rank)`` per entity, so an item
        present in both rankings outranks one that is merely first in a
        single ranking. Scores are normalised against the best fused score
        to keep entity scores in the same band as the pure-FTS path — the
        engine sorts entities and events together, so an unnormalised RRF
        value (~0.03) would push every entity below every event.

        Returns the fused rows plus the normalised relevance per entity ID.
        """
        fused: dict[str, float] = {}
        for rank, row in enumerate(fts_rows):
            fused[row["id"]] = fused.get(row["id"], 0.0) + 1.0 / (RRF_K + rank)
        for rank, entity_id in enumerate(vector_ids):
            fused[entity_id] = fused.get(entity_id, 0.0) + 1.0 / (RRF_K + rank)

        by_id = {row["id"]: row for row in fts_rows}
        missing = [eid for eid in fused if eid not in by_id]
        for row in self.repo.get_entities_by_ids(
            missing, types=types, include_stale=include_stale,
        ):
            by_id[row["id"]] = row

        # An ID can drop out here if get_entities_by_ids filtered it (type
        # or staleness); keep only rows we actually hold.
        ordered_ids = sorted(
            (eid for eid in fused if eid in by_id),
            key=lambda eid: fused[eid],
            reverse=True,
        )[:limit]

        best = max((fused[eid] for eid in ordered_ids), default=1.0) or 1.0
        relevance = {eid: fused[eid] / best for eid in ordered_ids}
        return [by_id[eid] for eid in ordered_ids], relevance
