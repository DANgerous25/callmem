"""Staleness detection for entities.

Periodically asks the local LLM whether newer entities supersede or
contradict older ones of the same type, and flags the older entity
as stale when so. The goal is to stop outdated context — e.g. an
old "auth uses JWT" fact that was later replaced by "auth uses
session cookies" — from flooding briefings and search.

The checker is scoped: it only compares *new* entities (those
created within ``lookback_minutes``) against *older* entities of the
same type. That keeps every run cheap and bounded, and avoids
rechecking pairs that were already resolved.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from callmem.core.json_utils import parse_json
from callmem.core.prompts import STALENESS_PROMPT
from callmem.core.repository import build_fts_match_query

if TYPE_CHECKING:
    from callmem.core.database import Database
    from callmem.core.ollama import OllamaClient

logger = logging.getLogger(__name__)

# How far back to look for "new" entities. Extraction runs every few
# seconds so 60 minutes comfortably covers any backlog; a larger
# window just means the check visits a few more candidates but the
# LLM call is short-circuited for already-stale / identical pairs.
DEFAULT_LOOKBACK_MINUTES = 60

# Maximum candidates (older siblings) inspected per new entity. The
# LLM call is O(candidates) per entity, so the cap matters.
DEFAULT_MAX_CANDIDATES = 5

# Only types where a newer entry can plausibly replace an older one.
# `change` / `research` entries describe moments, not durable state,
# so superseding them makes no sense.
_ELIGIBLE_TYPES = ("decision", "fact", "feature", "bugfix", "todo")


@dataclass
class StalenessDecision:
    older_id: str
    newer_id: str
    verdict: str
    reason: str


class StalenessChecker:
    """Detect superseded/contradicted entities and mark them stale."""

    def __init__(
        self,
        db: Database,
        ollama: OllamaClient | None,
        lookback_minutes: int = DEFAULT_LOOKBACK_MINUTES,
        max_candidates: int = DEFAULT_MAX_CANDIDATES,
    ) -> None:
        self.db = db
        self.ollama = ollama
        self.lookback_minutes = lookback_minutes
        self.max_candidates = max_candidates

    def run(self, project_id: str) -> list[StalenessDecision]:
        """Run one detection pass. Returns the decisions made."""
        if self.ollama is None:
            return []

        decisions: list[StalenessDecision] = []
        new_entities = self._load_new_entities(project_id)
        for new in new_entities:
            if new["type"] not in _ELIGIBLE_TYPES:
                continue
            candidates = self._find_candidates(project_id, new)
            for older in candidates:
                decision = self._judge(older, new)
                if decision is None:
                    continue
                decisions.append(decision)
                if decision.verdict in ("superseded", "contradicted"):
                    self._apply_decision(decision)
        return decisions

    # ── Internals ────────────────────────────────────────────────────

    def _load_new_entities(self, project_id: str) -> list[dict[str, Any]]:
        conn = self.db.connect()
        try:
            rows = conn.execute(
                "SELECT id, type, title, content, created_at "
                "FROM entities "
                "WHERE project_id = ? AND stale = 0 "
                "AND archived_at IS NULL "
                "AND created_at >= datetime('now', ?) "
                "ORDER BY created_at DESC",
                (project_id, f"-{self.lookback_minutes} minutes"),
            ).fetchall()
        finally:
            conn.close()
        return [dict(r) for r in rows]

    def _find_candidates(
        self, project_id: str, new: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Find older non-stale entities of the same type with shared
        keywords in title or content. Uses FTS5 so it's cheap."""
        query = _fts_query_from(new["title"], new["content"])
        if not query:
            return []

        conn = self.db.connect()
        try:
            try:
                rows = conn.execute(
                    "SELECT e.id, e.type, e.title, e.content, e.created_at "
                    "FROM entities_fts f "
                    "JOIN entities e ON e.rowid = f.rowid "
                    "WHERE entities_fts MATCH ? "
                    "AND e.project_id = ? AND e.type = ? "
                    "AND e.stale = 0 AND e.archived_at IS NULL "
                    "AND e.created_at < ? "
                    "AND e.id != ? "
                    "ORDER BY e.created_at DESC LIMIT ?",
                    (
                        query, project_id, new["type"], new["created_at"],
                        new["id"], self.max_candidates,
                    ),
                ).fetchall()
            except Exception as exc:  # pragma: no cover — bad FTS query
                logger.warning("FTS candidate query failed: %s", exc)
                return []
        finally:
            conn.close()
        return [dict(r) for r in rows]

    def _judge(
        self, older: dict[str, Any], newer: dict[str, Any],
    ) -> StalenessDecision | None:
        if self.ollama is None:  # pragma: no cover — guarded at run()
            return None

        prompt = STALENESS_PROMPT.format(
            etype=newer["type"],
            a_id=older["id"][:8], a_created=older.get("created_at", ""),
            a_title=older.get("title", ""),
            a_content=_truncate(older.get("content", ""), 400),
            b_id=newer["id"][:8], b_created=newer.get("created_at", ""),
            b_title=newer.get("title", ""),
            b_content=_truncate(newer.get("content", ""), 400),
        )

        try:
            raw = self.ollama.extract(prompt)
        except Exception as exc:
            logger.warning("Staleness LLM call failed: %s", exc)
            return None
        if not raw:
            return None

        try:
            data = parse_json(raw)
        except json.JSONDecodeError:
            logger.warning(
                "Staleness judge returned invalid JSON for pair "
                "%s/%s: %s",
                older["id"][:8], newer["id"][:8], raw[:200],
            )
            return None
        if not isinstance(data, dict):
            return None
        verdict = str(data.get("verdict", "")).strip().lower()
        reason = str(data.get("reason", "")).strip()
        if verdict not in {"superseded", "contradicted", "coexists"}:
            return None
        return StalenessDecision(
            older_id=older["id"], newer_id=newer["id"],
            verdict=verdict, reason=reason or verdict,
        )

    def _apply_decision(self, decision: StalenessDecision) -> None:
        conn = self.db.connect()
        try:
            conn.execute(
                "UPDATE entities "
                "SET stale = 1, staleness_reason = ?, superseded_by = ?, "
                "    updated_at = datetime('now') "
                "WHERE id = ? AND stale = 0",
                (decision.verdict, decision.newer_id, decision.older_id),
            )
            conn.commit()
        finally:
            conn.close()
        logger.info(
            "Marked entity %s stale (%s by %s)",
            decision.older_id[:8], decision.verdict,
            decision.newer_id[:8],
        )


def _truncate(value: str, limit: int) -> str:
    if value is None:
        return ""
    return value if len(value) <= limit else value[:limit] + "..."


def _fts_query_from(title: str, content: str) -> str:
    """Build a safe FTS5 MATCH query from the first few tokens of the
    title/content. Each token is quoted so hyphens, punctuation, or
    reserved words never reach the FTS5 parser as operators — that
    way a content line like ``cookie-backed sessions`` doesn't turn
    into ``cookie MINUS backed`` and blow up on ``no such column``.

    Delegates to the shared sanitizer in repository.py — see that
    module for the tokenizing logic itself.
    """
    return build_fts_match_query(
        title or "", content or "", min_len=4, max_tokens=6, joiner="OR",
    )
