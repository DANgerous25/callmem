"""Find and merge near-duplicate entities.

Within-session and cross-session deduplication of entities created by the
extractor. Operates on the existing schema (`entities.stale`,
`entities.superseded_by`, `entities.staleness_reason`) — we mark losers
stale rather than deleting them, so the original source-event linkage
survives for audit.

Strategy:
  1. Group candidates by (project_id, type) — only same-type duplicates
     count (a `discovery` and a `failure` about the same thing are kept
     separately, since they encode different lifecycle states).
  2. Bucket by a normalised title prefix to keep the pairwise comparison
     cheap on large projects.
  3. Inside each bucket, use difflib's quick ratio (cheap upper bound)
     then real ratio (precise) to find pairs above the similarity
     threshold.
  4. Union-find the matching pairs into clusters.
  5. In each cluster keep the OLDEST entity (most-established id) as the
     survivor; mark the rest stale with ``superseded_by = survivor.id``.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

logger = logging.getLogger(__name__)

# Tuned for the duplication patterns we see in real data: same concept
# captured as 3-4 entities with titles that differ by 1-2 words or are
# active/passive reformulations of each other.
DEFAULT_SIMILARITY_THRESHOLD = 0.82

# Bucket entities by the first N characters of their normalised title so
# we don't compare every pair globally. A bucket of 40 chars catches
# titles that share the same opening phrase, which is where almost all
# real duplicates cluster.
_BUCKET_PREFIX_LEN = 24

# Characters dropped during normalisation (punctuation, code markers).
_PUNCT_RE = re.compile(r"[^a-z0-9 ]+")
_MULTISPACE_RE = re.compile(r"\s+")


@dataclass
class DuplicateCluster:
    """A merge candidate: the survivor plus the entities to be retired."""
    survivor_id: str
    survivor_title: str
    losers: list[tuple[str, str]]  # [(id, title), …]

    @property
    def size(self) -> int:
        return 1 + len(self.losers)


def _normalise(title: str) -> str:
    """Lowercase, strip code-formatting backticks/quotes, collapse spaces."""
    s = (title or "").lower()
    s = _PUNCT_RE.sub(" ", s)
    s = _MULTISPACE_RE.sub(" ", s).strip()
    return s


_NUMBER_RE = re.compile(r"\d+")


def _number_tokens(s: str) -> tuple[str, ...]:
    """Tuple of all digit-runs in the string. Used to keep
    "migration to v3" and "migration to v4" from being merged — those
    titles differ only in a version digit, but they identify distinct
    work items.
    """
    return tuple(_NUMBER_RE.findall(s))


def _similar(a: str, b: str, threshold: float) -> bool:
    """Return True iff ``a`` and ``b`` are at least ``threshold`` similar.

    Uses SequenceMatcher's quick_ratio as a cheap upper bound first to
    skip the expensive ratio() on obvious non-matches. Additionally
    requires that any embedded numbers (e.g. version digits, port
    numbers, line numbers) match — otherwise titles that differ only by
    a digit get wrongly merged.
    """
    if not a or not b:
        return False
    if _number_tokens(a) != _number_tokens(b):
        return False
    matcher = SequenceMatcher(None, a, b)
    if matcher.quick_ratio() < threshold:
        return False
    return matcher.ratio() >= threshold


def find_clusters(
    db_path: Path,
    threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
    project_id: str | None = None,
    session_only: bool = False,
) -> list[DuplicateCluster]:
    """Discover near-duplicate entity clusters in the project DB.

    Parameters
    ----------
    threshold:
        Similarity floor (0..1). Real duplicates from the existing corpus
        match in the 0.85-0.95 range; below ~0.80 we start picking up
        legitimately distinct entities.
    project_id:
        Limit to a single project; defaults to all projects in the DB.
    session_only:
        When True, only group entities within the same session. Use this
        for the live within-session dedup pass; leave False for the
        one-shot retroactive sweep.
    """
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    try:
        sql = (
            "SELECT e.id, e.title, e.type, e.created_at, e.project_id, "
            "       ev.session_id "
            "FROM entities e LEFT JOIN events ev ON e.source_event_id = ev.id "
            "WHERE e.stale = 0 AND e.archived_at IS NULL"
        )
        params: list = []
        if project_id:
            sql += " AND e.project_id = ?"
            params.append(project_id)
        sql += " ORDER BY e.created_at ASC"
        rows = db.execute(sql, params).fetchall()
    finally:
        db.close()

    if not rows:
        return []

    # Group entities into buckets keyed by (project, type, [session,] prefix).
    buckets: dict[tuple, list[dict]] = {}
    for r in rows:
        d = dict(r)
        d["_norm"] = _normalise(d["title"])
        if not d["_norm"]:
            continue
        prefix = d["_norm"][:_BUCKET_PREFIX_LEN]
        key: tuple
        if session_only:
            key = (d["project_id"], d["type"], d["session_id"], prefix)
        else:
            key = (d["project_id"], d["type"], prefix)
        buckets.setdefault(key, []).append(d)

    # Union-find over similar pairs within each bucket.
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        while parent.get(x, x) != x:
            parent[x] = parent.get(parent[x], parent[x])
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra == rb:
            return
        # Keep the earlier-created entity as the canonical root, since
        # iteration order followed created_at ASC.
        parent[rb] = ra

    entity_index: dict[str, dict] = {}
    for bucket in buckets.values():
        if len(bucket) < 2:
            continue
        for ent in bucket:
            entity_index[ent["id"]] = ent
            parent.setdefault(ent["id"], ent["id"])
        for i in range(len(bucket)):
            for j in range(i + 1, len(bucket)):
                a, b = bucket[i], bucket[j]
                if _similar(a["_norm"], b["_norm"], threshold):
                    union(a["id"], b["id"])

    # Collect clusters from union-find roots.
    cluster_members: dict[str, list[dict]] = {}
    for eid in parent:
        root = find(eid)
        cluster_members.setdefault(root, []).append(entity_index[eid])

    clusters: list[DuplicateCluster] = []
    for root_id, members in cluster_members.items():
        if len(members) < 2:
            continue
        members.sort(key=lambda m: m["created_at"])
        survivor = members[0]
        losers = [(m["id"], m["title"]) for m in members[1:]]
        clusters.append(DuplicateCluster(
            survivor_id=survivor["id"],
            survivor_title=survivor["title"],
            losers=losers,
        ))
    return clusters


def apply_clusters(
    db_path: Path,
    clusters: list[DuplicateCluster],
    dry_run: bool = False,
) -> int:
    """Mark losers stale and link them to the survivor.

    Returns the number of entities marked stale. When ``dry_run`` is True
    no DB writes occur — useful for inspection before committing.
    """
    if not clusters or dry_run:
        return sum(len(c.losers) for c in clusters)

    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    marked = 0
    try:
        with db:  # transaction
            for cluster in clusters:
                for loser_id, _ in cluster.losers:
                    db.execute(
                        "UPDATE entities SET stale = 1, "
                        "  superseded_by = ?, "
                        "  staleness_reason = 'duplicate of ' || ?, "
                        "  updated_at = CURRENT_TIMESTAMP "
                        "WHERE id = ? AND stale = 0",
                        (cluster.survivor_id, cluster.survivor_id, loser_id),
                    )
                    marked += 1
    finally:
        db.close()
    return marked
