"""Usage analytics for callmem.

Aggregates per-session memory usage signals from the events table so we
can answer the honest questions: is memory being read, is it being cited
in responses, and what does it cost.

The metrics here intentionally avoid claiming "tokens saved" — that
requires a controlled A/B comparison, not introspection of one stream.
What we *can* measure: read calls, write calls, briefing fetches, entity
citation counts in agent responses, and total session token cost.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from callmem.compat import UTC

if TYPE_CHECKING:
    from callmem.core.database import Database

# Tools that read from memory (consumption signals)
_READ_TOOLS = (
    "mem_get_briefing",
    "mem_search",
    "mem_search_by_file",
    "mem_search_index",
    "mem_get_entities",
    "mem_get_tasks",
    "mem_check_context",
    "mem_timeline",
    "mem_file_context",
)

# Tools that write to / curate memory
_WRITE_TOOLS = (
    "mem_ingest",
    "mem_pin",
    "mem_mark_current",
    "mem_mark_stale",
    "mem_compress_context",
    "mem_session_start",
    "mem_session_end",
    "mem_vault_review",
)

# Entity-ID short form: the last 8 ULID characters. We match candidates
# with this regex, then verify each one against the real entity short-id
# set for the project — that filters out session IDs (which start with
# the timestamp prefix `01K…`), placeholder strings like `#XXXXXXXX`
# from documentation, and other near-misses. Crockford base32 excludes
# I/L/O/U so genuine ULID chars are A–H, J, K, M, N, P–T, V–Z, 0–9.
_CITATION_RE = re.compile(r"#[A-Z0-9]{8}\b")


def _load_entity_id_map(
    db: sqlite3.Connection, project_id: str | None = None,
) -> dict[str, str]:
    """Map each entity's citable short ID (last 8 chars) to its full ULID.

    Stored entity IDs are full ULIDs; the briefing surfaces the last 8
    characters as the citable form, so that's what we match against. The
    full ID is what per-entity citation persistence needs to update the
    right row.

    Scoped to ``project_id`` when given. The session-end hot path
    (``compute_session_citations``) always passes it — an unscoped
    ``SELECT id FROM entities`` is an O(every entity in every project
    sharing this db file) scan on every session end, and it also risks a
    citation in one project's session getting attributed to a different
    project's entity that happens to share the same 8-char short-ID
    suffix. The CLI's full-history backfill (``compute_entity_citations``)
    has no project_id in hand and keeps the unscoped scan — a one-off,
    operator-invoked pass, not a per-session cost.
    """
    if project_id is not None:
        rows = db.execute(
            "SELECT id FROM entities WHERE project_id = ?", (project_id,),
        ).fetchall()
    else:
        rows = db.execute("SELECT id FROM entities").fetchall()
    return {row[0][-8:]: row[0] for row in rows if row[0]}


def _extract_cited_entity_ids(
    content: str | None, entity_id_map: dict[str, str],
) -> list[str]:
    """Return the full entity IDs cited in a response event's content.

    One list entry per citation occurrence (duplicates preserved), so
    callers can both count total citations and tally per-entity totals
    from the same pass. Filters out candidates that don't resolve to a
    real entity — session refs, doc placeholders, near-misses.
    """
    if not content:
        return []
    return [
        full_id
        for m in _CITATION_RE.finditer(content)
        if (full_id := entity_id_map.get(m.group()[1:])) is not None
    ]


def _estimate_tokens(text: str | None) -> int:
    """Cheap token estimator (~4 chars/token) used when events.token_count
    is unpopulated, as it is across the existing event corpus.
    """
    if not text:
        return 0
    return len(text) // 4


@dataclass
class SessionUsage:
    session_id: str
    started_at: str
    agent_name: str | None
    model_name: str | None
    event_count: int
    session_tokens: int
    briefing_fetched: bool
    mem_reads: int
    mem_writes: int
    citations: int

    @property
    def short_id(self) -> str:
        return self.session_id[-10:]

    @property
    def memory_used(self) -> bool:
        """True if any read-side memory tool was invoked OR any entity
        citation appears in agent responses. This is the closest honest
        proxy we have for "the agent consulted memory in this session."
        """
        return self.mem_reads > 0 or self.citations > 0


def _parse_since(since: str | None) -> str | None:
    """Parse a duration string ('7d', '24h', '30d', 'all') into an ISO
    cutoff timestamp. Returns None when no filter applies.
    """
    if not since or since.lower() == "all":
        return None
    s = since.strip().lower()
    if s.endswith("d"):
        days = int(s[:-1])
        cutoff = datetime.now(UTC) - timedelta(days=days)
    elif s.endswith("h"):
        hours = int(s[:-1])
        cutoff = datetime.now(UTC) - timedelta(hours=hours)
    else:
        raise ValueError(f"Unrecognised --since value: {since!r}")
    return cutoff.isoformat()


def _count_in_content(content: str | None, needles: tuple[str, ...]) -> int:
    if not content:
        return 0
    return sum(content.count(n) for n in needles)


def collect_session_usage(
    db_path: Path,
    since: str | None = None,
) -> list[SessionUsage]:
    """Walk every session in the project and compute usage signals."""
    cutoff = _parse_since(since)
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    try:
        entity_id_map = _load_entity_id_map(db)
        sql = (
            "SELECT id, started_at, agent_name, model_name, event_count "
            "FROM sessions"
        )
        params: tuple[Any, ...] = ()
        if cutoff:
            sql += " WHERE started_at >= ?"
            params = (cutoff,)
        sql += " ORDER BY started_at DESC"
        sessions = db.execute(sql, params).fetchall()

        results: list[SessionUsage] = []
        for s in sessions:
            sid = s["id"]
            events = db.execute(
                "SELECT type, content, token_count FROM events WHERE session_id = ?",
                (sid,),
            ).fetchall()
            session_tokens = 0
            mem_reads = 0
            mem_writes = 0
            briefing_fetched = False
            citations = 0
            for ev in events:
                c = ev["content"] or ""
                # Fall back to estimating from content when token_count is
                # 0/null (true for the entire existing corpus pre-instrument).
                session_tokens += (ev["token_count"] or 0) or _estimate_tokens(c)
                t = ev["type"]
                if t == "tool_call":
                    mem_reads += _count_in_content(c, _READ_TOOLS)
                    mem_writes += _count_in_content(c, _WRITE_TOOLS)
                    if "mem_get_briefing" in c:
                        briefing_fetched = True
                if t == "response":
                    citations += len(_extract_cited_entity_ids(c, entity_id_map))
            results.append(SessionUsage(
                session_id=sid,
                started_at=s["started_at"],
                agent_name=s["agent_name"],
                model_name=s["model_name"],
                event_count=s["event_count"] or 0,
                session_tokens=session_tokens,
                briefing_fetched=briefing_fetched,
                mem_reads=mem_reads,
                mem_writes=mem_writes,
                citations=citations,
            ))
        return results
    finally:
        db.close()


def _tally_citations(
    conn: sqlite3.Connection,
    where_extra: str = "",
    params: tuple[Any, ...] = (),
    project_id: str | None = None,
) -> dict[str, tuple[int, str]]:
    """Scan response events (optionally narrowed by ``where_extra``) and
    tally citations per entity.

    Shared by the full-history scan (``compute_entity_citations``) and the
    session-scoped scan (``compute_session_citations``) so both use the
    exact same detection logic. Returns ``{entity_id: (cited_count,
    last_cited_at)}`` — entities not cited within the scanned rows are
    simply absent; callers should treat a missing entry as ``(0, None)``.
    ``project_id`` scopes the entity short-ID lookup (see
    _load_entity_id_map) — pass it whenever the caller has one.
    """
    entity_id_map = _load_entity_id_map(conn, project_id)
    rows = conn.execute(
        f"SELECT content, timestamp FROM events WHERE type = 'response'{where_extra}",
        params,
    ).fetchall()
    counts: dict[str, int] = {}
    last_seen: dict[str, str] = {}
    for row in rows:
        ts = row["timestamp"] or ""
        for entity_id in _extract_cited_entity_ids(row["content"], entity_id_map):
            counts[entity_id] = counts.get(entity_id, 0) + 1
            if ts and (entity_id not in last_seen or ts > last_seen[entity_id]):
                last_seen[entity_id] = ts
    return {
        entity_id: (count, last_seen.get(entity_id, ""))
        for entity_id, count in counts.items()
    }


def compute_entity_citations(db_path: Path) -> dict[str, tuple[int, str]]:
    """Scan every response event in the database and tally citations per
    entity — the full-history backfill scan, driven by a bare db path
    (matches how ``collect_session_usage`` is already invoked from the
    CLI). See ``compute_session_citations`` for the cheap, session-scoped
    counterpart used on every session end.
    """
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    try:
        return _tally_citations(db)
    finally:
        db.close()


def compute_session_citations(
    db: Database, session_id: str,
) -> dict[str, tuple[int, str]]:
    """Scan a single session's response events and tally citations per
    entity.

    Session-scoped counterpart to ``compute_entity_citations`` — cheap
    enough to run on every session end without a full-history rescan.
    Takes a ``Database`` (not a bare path) so it works correctly against
    both file-backed and shared-cache in-memory databases, matching how
    the rest of the engine/repository access the DB.

    The entity short-ID lookup is scoped to the session's own project
    (resolved from the sessions row) — without that, a hot-path session
    end would do an unscoped scan of every entity in every project
    sharing this db file, and a citation could get attributed to the
    wrong project's entity if two share a short-ID suffix.
    """
    conn = db.connect()
    try:
        session_row = conn.execute(
            "SELECT project_id FROM sessions WHERE id = ?", (session_id,),
        ).fetchone()
        if session_row is None:
            return {}
        return _tally_citations(
            conn, " AND session_id = ?", (session_id,),
            project_id=session_row["project_id"],
        )
    finally:
        conn.close()


def backfill_citation_counts(db_path: Path) -> int:
    """Persist per-entity citation counts computed from the full event
    history into the entities table.

    Idempotent: recomputes absolute counts from a fresh scan each call
    rather than incrementing, so re-running (e.g. every ``callmem usage``
    invocation, the "existing usage-scan path") never double-counts.
    Returns the number of entity rows updated. This is the manual/bulk
    path — session end persists citations incrementally on its own (see
    ``compute_session_citations`` + ``Repository.increment_citation_counts``
    in ``engine.end_session``), so this remains for backfilling projects
    that predate that hook, or for anyone who wants an authoritative
    recount.
    """
    from callmem.core.database import Database
    from callmem.core.repository import Repository

    citations = compute_entity_citations(db_path)
    if not citations:
        return 0
    repo = Repository(Database(db_path))
    return repo.set_citation_counts(citations)


@dataclass
class UsageSummary:
    project: str
    session_count: int
    sessions_with_memory_used: int
    sessions_with_briefing_fetched: int
    total_mem_reads: int
    total_mem_writes: int
    total_citations: int
    total_session_tokens: int

    @property
    def usage_rate(self) -> float:
        if not self.session_count:
            return 0.0
        return self.sessions_with_memory_used / self.session_count

    @property
    def briefing_rate(self) -> float:
        if not self.session_count:
            return 0.0
        return self.sessions_with_briefing_fetched / self.session_count


def summarise(project_name: str, usages: list[SessionUsage]) -> UsageSummary:
    return UsageSummary(
        project=project_name,
        session_count=len(usages),
        sessions_with_memory_used=sum(1 for u in usages if u.memory_used),
        sessions_with_briefing_fetched=sum(1 for u in usages if u.briefing_fetched),
        total_mem_reads=sum(u.mem_reads for u in usages),
        total_mem_writes=sum(u.mem_writes for u in usages),
        total_citations=sum(u.citations for u in usages),
        total_session_tokens=sum(u.session_tokens for u in usages),
    )
