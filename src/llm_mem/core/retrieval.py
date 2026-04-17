"""Multi-strategy retrieval engine.

Combines structured lookup, FTS5 search, and recency weighting
to find relevant memories.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from llm_mem.compat import UTC

if TYPE_CHECKING:
    from llm_mem.core.repository import Repository
    from llm_mem.models.config import Config

RECENCY_HALF_LIFE_DAYS = 7.0
DEFAULT_STRATEGIES = ("fts", "entities")


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


def _recency_factor(timestamp: str, now: str | None = None) -> float:
    """Exponential decay: recent items score higher.

    Half-life is RECENCY_HALF_LIFE_DAYS days.
    """
    if not timestamp:
        return 1.0
    try:
        ts = datetime.fromisoformat(timestamp)
    except (ValueError, TypeError):
        return 1.0
    reference = datetime.fromisoformat(now) if now else datetime.now(UTC)
    age_days = max(0.0, (reference - ts).total_seconds() / 86400)
    return math.exp(-0.693 * age_days / RECENCY_HALF_LIFE_DAYS)


def _estimate_tokens(text: str) -> int:
    """Rough token estimation: ~4 chars per token."""
    return max(1, len(text) // 4)


class RetrievalEngine:
    """Multi-strategy retrieval over events, entities, and summaries."""

    def __init__(self, repo: Repository, config: Config) -> None:
        self.repo = repo
        self.config = config

    def search(
        self,
        project_id: str,
        query: str,
        types: list[str] | None = None,
        session_id: str | None = None,
        limit: int = 20,
        include_archived: bool = False,
        strategies: list[str] | None = None,
    ) -> list[SearchResult]:
        """Search across events and entities using multiple strategies."""
        active_strategies = strategies or list(DEFAULT_STRATEGIES)
        results: dict[str, SearchResult] = {}

        if "fts" in active_strategies and query:
            self._search_fts(
                project_id, query, session_id, limit, results, types
            )

        if "entities" in active_strategies:
            self._search_entities(
                project_id, query, types, session_id, limit, results
            )

        ranked = sorted(results.values(), key=lambda r: r.score, reverse=True)

        if not include_archived:
            ranked = [r for r in ranked if r.metadata.get("archived_at") is None]

        return ranked[:limit]

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
        conn = self.repo.db.connect()
        try:
            rows = conn.execute(
                "SELECT e.id, e.type, e.content, e.timestamp, e.session_id, "
                "e.metadata, e.archived_at "
                "FROM events_fts f "
                "JOIN events e ON e.rowid = f.rowid "
                "WHERE events_fts MATCH ? AND e.project_id = ? "
                "ORDER BY rank LIMIT ?",
                (query, project_id, limit),
            ).fetchall()
        finally:
            conn.close()

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
                metadata={"archived_at": r["archived_at"]},
            )

    def _search_entities(
        self,
        project_id: str,
        query: str,
        types: list[str] | None,
        session_id: str | None,
        limit: int,
        results: dict[str, SearchResult],
    ) -> None:
        conn = self.repo.db.connect()
        try:
            clauses: list[str] = ["project_id = ?"]
            params: list[Any] = [project_id]

            if types:
                placeholders = ",".join("?" for _ in types)
                clauses.append(f"type IN ({placeholders})")
                params.extend(types)

            where = " AND ".join(clauses)
            rows = conn.execute(
                f"SELECT * FROM entities WHERE {where} "
                f"ORDER BY pinned DESC, updated_at DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
        finally:
            conn.close()

        now = datetime.now(UTC).isoformat()
        query_lower = query.lower() if query else ""

        for r in rows:
            if query_lower:
                title = (r["title"] or "").lower()
                content = (r["content"] or "").lower()
                if query_lower not in title and query_lower not in content:
                    continue

            recency = _recency_factor(r["updated_at"], now)
            pin_boost = 1.5 if r["pinned"] else 1.0
            score = 0.8 * recency * pin_boost

            results[r["id"]] = SearchResult(
                id=r["id"],
                source_type="entity",
                type=r["type"],
                title=r["title"],
                content=r["content"],
                score=score,
                timestamp=r["updated_at"],
                session_id=None,
                metadata={"status": r["status"], "priority": r["priority"]},
            )
