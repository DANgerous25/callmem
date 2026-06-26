"""Startup briefing generator.

Assembles a compact context block from recent memories,
active TODOs, decisions, and summaries with rich visual formatting.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from callmem import __version__ as _CALLMEM_VERSION
from callmem.compat import UTC
from callmem.core.retrieval import _estimate_tokens

if TYPE_CHECKING:
    from callmem.core.ollama import OllamaClient
    from callmem.core.repository import Repository
    from callmem.models.config import Config

logger = logging.getLogger(__name__)

# How long to trust a cached PyPI lookup before re-checking
_UPDATE_CHECK_TTL_SECONDS = 86_400  # 24h
_UPDATE_CHECK_TIMEOUT = 2.0  # network call must be fast or skipped
_PYPI_JSON_URL = "https://pypi.org/pypi/callmem/json"

# Project overview is always shown at the top of the briefing but excluded
# from the token-budget calculation — it is capped independently here.
_OVERVIEW_MAX_TOKENS = 500


CATEGORY_EMOJI: dict[str, str] = {
    "feature": "\U0001f7e2",
    "bugfix": "\U0001f534",
    "discovery": "\U0001f535",
    "decision": "\u2696\ufe0f",
    "todo": "\U0001f4cb",
    "failure": "\u274c",
    "research": "\U0001f52c",
    "change": "\U0001f504",
    "fact": "\U0001f4dd",
}

LEGEND_ORDER = [
    "feature", "bugfix", "discovery", "decision",
    "todo", "failure", "research", "change", "fact",
]

# Box-drawing characters for visual structure
_BOX_H = "\u2500"  # ─
_BOX_TL = "\u256d"  # ╭
_BOX_TR = "\u256e"  # ╮
_BOX_BL = "\u2570"  # ╰
_BOX_BR = "\u256f"  # ╯
_BOX_V = "\u2502"  # │
_BULLET = "\u25cf"  # ●
_ARROW = "\u25b6"  # ▶


@dataclass
class Briefing:
    project_name: str
    content: str
    token_count: int
    components: dict[str, int] = field(default_factory=dict)
    generated_at: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat()
    )
    observations_loaded: int = 0
    read_tokens: int = 0
    work_investment: int = 0
    savings_pct: float = 0.0


NEW_PROJECT_MESSAGE = (
    "[{project_name}] recent context, {datetime}\n"
    + "\u2500" * 48 + "\n\n"
    "No prior memories found. This is a new project."
)


def _truncate_to_tokens(text: str, max_tokens: int) -> str:
    if max_tokens <= 0:
        return ""
    max_chars = max_tokens * 4
    if len(text) <= max_chars:
        return text
    return text[:max_chars - 3] + "..."


def _short_id(full_id: str | None) -> str:
    """Return a short, display-friendly ID suffix.

    ULIDs share a timestamp prefix when created in the same millisecond,
    so we use the random suffix to avoid display collisions. The briefing
    advertises this as the ID form, and mem_get_entities accepts it via
    LIKE matching.
    """
    if not full_id:
        return ""
    return full_id[-8:]


def _session_hook(session: dict[str, Any], max_chars: int = 70) -> str:
    """Extract a single-line session hook from the summary field."""
    summary = (session.get("summary") or "").strip()
    if not summary:
        return ""
    first_line = summary.split("\n", 1)[0].strip()
    if len(first_line) > max_chars:
        first_line = first_line[: max_chars - 1].rstrip() + "\u2026"
    return first_line


def _parse_version(s: str) -> tuple[int, ...] | None:
    """Parse a dot-separated version into a tuple of ints.

    Returns None for non-conforming strings (pre-release tags, ``+local`` suffixes,
    or anything we'd rather not compare against). The briefing prefers to stay
    quiet over misreporting an update.
    """
    if not s:
        return None
    head = s.split("+", 1)[0].split("-", 1)[0]
    try:
        return tuple(int(p) for p in head.split("."))
    except ValueError:
        return None


def _check_latest_callmem_version(cache_dir: Path) -> str | None:
    """Return the latest released callmem version on PyPI, or None.

    Cached under ``<cache_dir>/.update_check.json`` for ``_UPDATE_CHECK_TTL_SECONDS``.
    Any failure (offline, timeout, parse error) returns None silently \u2014 the
    briefing is the wrong place to surface networking noise.
    """
    cache_path = cache_dir / ".update_check.json"
    now = datetime.now(UTC).timestamp()
    try:
        cached = json.loads(cache_path.read_text())
        if now - float(cached.get("checked_at", 0)) < _UPDATE_CHECK_TTL_SECONDS:
            latest = cached.get("latest")
            return str(latest) if latest else None
    except (OSError, ValueError, TypeError):
        pass

    try:
        import httpx
        resp = httpx.get(_PYPI_JSON_URL, timeout=_UPDATE_CHECK_TIMEOUT)
        resp.raise_for_status()
        latest = resp.json().get("info", {}).get("version")
    except Exception as exc:  # noqa: BLE001 \u2014 best-effort, never fail briefing
        logger.debug("PyPI version check failed: %s", exc)
        latest = None

    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps({"checked_at": now, "latest": latest}))
    except OSError as exc:
        logger.debug("Could not write update cache: %s", exc)

    return latest


class BriefingGenerator:
    def __init__(
        self,
        repo: Repository,
        config: Config,
        ollama: OllamaClient | None = None,
    ) -> None:
        self.repo = repo
        self.config = config
        self.ollama = ollama

    def generate(
        self,
        project_id: str,
        project_name: str = "default",
        max_tokens: int | None = None,
        focus: str | None = None,
    ) -> Briefing:
        budget = max_tokens or self.config.briefing.max_tokens

        all_entities, suppressed_stale = self._fetch_all_entities(
            project_id, focus,
        )
        sessions = self._fetch_sessions_with_entities(project_id)
        last_session = self._fetch_last_session(project_id)
        work_investment = self._compute_work_investment(project_id)
        self._suppressed_stale_count = suppressed_stale

        # Project overview — fetched once, rendered at the top of the
        # briefing but excluded from the token budget (it is independently
        # capped at _OVERVIEW_MAX_TOKENS).
        overview_block = self._build_overview_block(project_id, w=58)
        overview_tokens = _estimate_tokens(overview_block) if overview_block else 0

        if not all_entities and not last_session:
            now_str = datetime.now(UTC).strftime("%Y-%m-%d %-I:%M%p")
            event_count = self._fetch_event_count(project_id)
            if event_count > 0:
                content = self._build_extraction_warning(
                    project_name, now_str, event_count,
                )
            else:
                content = NEW_PROJECT_MESSAGE.format(
                    project_name=project_name, datetime=now_str
                )
            if overview_block:
                content = overview_block + "\n" + content
            return Briefing(
                project_name=project_name,
                content=content,
                token_count=_estimate_tokens(content),
                components={"new_project": 1},
            )

        observations_loaded = len(all_entities)
        read_tokens = sum(
            _estimate_tokens(e.get("content") or "") for e in all_entities
        )

        # Two-pass render: pass 1 measures the rendered briefing so
        # "compression savings" can report the honest ratio of what the
        # agent actually reads (the final briefing, including its
        # envelope and the 2000-token budget truncation) vs. the total
        # captured work. Pass 2 re-renders with the correct percentage.
        # The tail — Suggested next + footer — is rendered separately
        # and never truncated, so a fresh agent always sees the
        # curated pickup list and the Web UI URL.
        w = 58
        provisional_body = "\n".join(self._build_briefing_parts(
            project_name, all_entities, sessions,
            last_session, observations_loaded, read_tokens,
            work_investment, 0.0, overview_block,
        ))
        suggested_next_str = "\n".join(
            self._build_suggested_next_parts(all_entities, w)
        )
        # Render the provisional footer using `budget` as a placeholder for
        # briefing_tokens so it hits the same layout branch as the final
        # footer (otherwise the body budget would be computed against a
        # shorter footer and the assembled briefing would overshoot).
        provisional_footer = "\n".join(self._build_footer_parts(
            work_investment, observations_loaded, budget, w,
        ))
        tail_tokens = (
            _estimate_tokens(provisional_footer)
            + (_estimate_tokens(suggested_next_str) if suggested_next_str else 0)
        )
        # The overview is always shown and excluded from the body budget,
        # so add its tokens back in — the entity/history body keeps its
        # full share of the token budget.
        body_budget = max(budget - tail_tokens + overview_tokens, 0)
        if _estimate_tokens(provisional_body) > body_budget:
            provisional_body = _truncate_to_tokens(provisional_body, body_budget)
        briefing_tokens = _estimate_tokens(provisional_body) + tail_tokens

        # savings_pct kept for backwards-compat in components dict only — it is
        # no longer rendered in the footer. The previous "captured/saved %" line
        # was misleading (it was compression ratio against work the agent would
        # never have read anyway, not real savings).
        if work_investment > 0 and briefing_tokens > 0:
            savings_pct = round(
                (1 - briefing_tokens / work_investment) * 100, 1
            )
        else:
            savings_pct = 0.0

        body = "\n".join(self._build_briefing_parts(
            project_name, all_entities, sessions,
            last_session, observations_loaded, read_tokens,
            work_investment, savings_pct, overview_block,
        ))
        footer = "\n".join(self._build_footer_parts(
            work_investment, observations_loaded, briefing_tokens, w,
        ))
        if _estimate_tokens(body) > body_budget:
            body = _truncate_to_tokens(body, body_budget)
        if suggested_next_str:
            assembled = body + "\n" + suggested_next_str + "\n" + footer
        else:
            assembled = body + "\n" + footer
        # Final guard: the two-pass render can still overshoot by a token
        # or two (savings_pct can change the Context Economics line
        # width; join-newlines aren't counted in tail_tokens). Trim the
        # body further if assembled exceeds the budget, preserving the
        # protected tail.
        overflow = _estimate_tokens(assembled) - budget
        if overflow > 0:
            body = _truncate_to_tokens(body, max(body_budget - overflow, 0))
            if suggested_next_str:
                assembled = body + "\n" + suggested_next_str + "\n" + footer
            else:
                assembled = body + "\n" + footer

        components = {}
        if all_entities:
            components["entities"] = len(all_entities)
        if last_session:
            components["last_session"] = 1

        return Briefing(
            project_name=project_name,
            content=assembled,
            token_count=_estimate_tokens(assembled),
            components=components,
            observations_loaded=observations_loaded,
            read_tokens=read_tokens,
            work_investment=work_investment,
            savings_pct=savings_pct,
        )

    def write_session_summary(
        self,
        project_id: str,
        project_name: str,
        worktree_path: str | Path,
        max_tokens: int | None = None,
    ) -> Briefing:
        from pathlib import Path

        briefing = self.generate(project_id, project_name, max_tokens)
        summary_path = Path(worktree_path) / "SESSION_SUMMARY.md"
        summary_path.write_text(briefing.content, encoding="utf-8")
        return briefing

    def _build_briefing_parts(
        self,
        project_name: str,
        entities: list[dict[str, Any]],
        sessions: list[dict[str, Any]],
        last_session: dict[str, Any] | None,
        observations_loaded: int,
        read_tokens: int,
        work_investment: int,
        savings_pct: float,
        overview_block: str = "",
    ) -> list[str]:
        parts: list[str] = []
        w = 58  # box width

        now_str = datetime.now(UTC).strftime("%Y-%m-%d %-I:%M%p")

        # ── Header box ──
        title = f" {_ARROW} {project_name} "
        date_str = f" {now_str} "
        pad = w - 2 - len(title) - len(date_str)
        if pad < 1:
            pad = 1

        parts.append(f"{_BOX_TL}{_BOX_H * (w - 2)}{_BOX_TR}")
        parts.append(f"{_BOX_V}{title}{' ' * pad}{date_str}{_BOX_V}")
        parts.append(f"{_BOX_BL}{_BOX_H * (w - 2)}{_BOX_BR}")
        parts.append("")

        # ── Project overview (always-visible, excluded from budget) ──
        if overview_block:
            parts.append(overview_block)
            parts.append("")

        # ── Context economics ──
        parts.append(f"{_BOX_H * 3} Context Economics {_BOX_H * (w - 22)}")
        parts.append("")
        parts.append(
            f"  {_BULLET} {observations_loaded} observations loaded "
            f"({read_tokens:,} tokens of entity content)"
        )
        parts.append(
            f"  {_BULLET} {work_investment:,} tokens of captured work"
        )
        if savings_pct > 0:
            parts.append(
                f"  {_BULLET} {savings_pct}% savings — briefing "
                f"compresses project history to fit the token budget"
            )
        parts.append("")

        # ── Legend ──
        legend_parts = []
        for cat in LEGEND_ORDER:
            emoji = CATEGORY_EMOJI.get(cat, "")
            legend_parts.append(f"{emoji} {cat}")
        sep = f" {_BOX_V} "
        parts.append(f"  {sep.join(legend_parts)}")
        parts.append("")

        # ── How to use this ──
        parts.append(
            f"{_BOX_H * 3} How to use this {_BOX_H * (w - 20)}"
        )
        parts.append("")
        parts.append(
            "  This index is usually enough to understand past work."
        )
        parts.append(
            "  For full content: mem_get_entities(ids=[\"F5AVDQ25\"])"
        )
        parts.append(
            "  For past research/bugs/decisions: mem_search(query=...)"
        )
        parts.append(
            "  Trust this index over re-reading code for past decisions."
        )
        parts.append("")

        # ── Latest session: narrative block (prioritized at top) ──
        if last_session:
            self._append_latest_session_block(
                parts, last_session, sessions, w,
            )

        # ── Open TODOs and failures (high priority) ──
        todos = [
            e for e in entities
            if e.get("type") == "todo"
            and e.get("status") != "resolved"
        ]
        failures = [
            e for e in entities
            if e.get("type") == "failure"
            and e.get("status") != "resolved"
        ]

        if todos or failures:
            parts.append(
                f"{_BOX_H * 3} Action Items "
                f"{_BOX_H * (w - 18)}"
            )
            parts.append("")
            for e in failures:
                eid = _short_id(e.get("id"))
                title = e.get("title") or e.get("content", "")[:60]
                parts.append(f"  #{eid}  \u274c  {title}")
            for e in todos:
                eid = _short_id(e.get("id"))
                title = e.get("title") or e.get("content", "")[:60]
                priority = e.get("priority", "")
                flag = f" [{priority}]" if priority else ""
                parts.append(f"  #{eid}  \U0001f4cb  {title}{flag}")
            parts.append("")

        # ── Observations by date ──
        entities_by_date = self._group_entities_by_date(entities)
        for date_label, date_entities in entities_by_date.items():
            parts.append(
                f"{_BOX_H * 3} {date_label} "
                f"{_BOX_H * (w - 6 - len(date_label))}"
            )
            parts.append("")

            by_session = self._group_by_session(date_entities, sessions)
            for session_header, session_entities in by_session:
                parts.append(f"  {_ARROW} {session_header}")
                for e in session_entities:
                    eid = _short_id(e.get("id"))
                    emoji = CATEGORY_EMOJI.get(e.get("type", ""), "")
                    title = e.get("title") or ""
                    parts.append(f"    #{eid}  {emoji}  {title}")
                parts.append("")

        # Body ends here; the caller appends Suggested next + footer
        # after truncation so both survive the token-budget trim.
        return parts

    def _build_suggested_next_parts(
        self,
        entities: list[dict[str, Any]],
        w: int,
    ) -> list[str]:
        failures = [
            e for e in entities
            if e.get("type") == "failure"
            and e.get("status") != "resolved"
        ]
        todos = [
            e for e in entities
            if e.get("type") == "todo"
            and e.get("status") != "resolved"
        ]
        high = [t for t in todos if t.get("priority") == "high"]
        medium = [t for t in todos if t.get("priority") == "medium"]
        candidates = (failures + high + medium)[:5]
        if not candidates:
            return []

        parts: list[str] = []
        header = " Suggested next "
        parts.append(f"{_BOX_H * 3}{header}{_BOX_H * (w - 3 - len(header))}")
        parts.append("")
        for e in candidates:
            eid = _short_id(e.get("id"))
            title = e.get("title") or e.get("content", "")[:60]
            if e.get("type") == "failure":
                parts.append(f"  #{eid}  ❌  {title}")
            else:
                priority = e.get("priority", "")
                flag = f" [{priority}]" if priority else ""
                parts.append(f"  #{eid}  \U0001f4cb  {title}{flag}")
        parts.append("")
        return parts

    def _build_footer_parts(
        self,
        work_investment: int,
        entity_count: int,
        briefing_tokens: int,
        w: int,
    ) -> list[str]:
        """Footer block (stats + optional update notice + Web UI URL).

        Always rendered last and never truncated, so the user can rely on
        the Web UI URL being the final line of the briefing.

        Stats are intentionally factual, not derived: the briefing token
        cost, the number of entities surfaced, and the raw captured size
        — no "compression savings" claim, since the agent would not have
        read raw events directly.
        """
        parts: list[str] = []
        parts.append(f"{_BOX_H * w}")
        if briefing_tokens:
            parts.append(
                f"  briefing: {briefing_tokens:,}t "
                f"{_BOX_V} {entity_count} entities surfaced "
                f"{_BOX_V} {work_investment:,}t captured (full history in Web UI)"
            )
        else:
            parts.append(
                f"  {entity_count} entities surfaced "
                f"{_BOX_V} {work_investment:,}t captured (full history in Web UI)"
            )
        suppressed = getattr(self, "_suppressed_stale_count", 0) or 0
        if suppressed:
            parts.append(
                f"  ({suppressed} stale {'entity' if suppressed == 1 else 'entities'} "
                f"suppressed — run 'callmem stale' to review)"
            )

        cache_dir = self.repo.db.db_path.parent
        latest = _check_latest_callmem_version(cache_dir)
        current = _parse_version(_CALLMEM_VERSION)
        latest_parsed = _parse_version(latest or "")
        if latest and current and latest_parsed and latest_parsed > current:
            parts.append(
                f"  📦 Update available: callmem {_CALLMEM_VERSION} → {latest} "
                f"(pip install -U callmem)"
            )

        ui_port = self.config.ui.port
        ui_host = self.config.ui.host
        parts.append(f"  Web UI: http://{ui_host}:{ui_port}")
        return parts

    def _build_overview_block(
        self, project_id: str, w: int,
    ) -> str:
        """Fetch the project overview and format it as a briefing block.

        Returns an empty string if no overview is set, so the caller can
        skip the section silently.
        """
        row = self.repo.get_overview(project_id)
        if row is None:
            return ""
        content = (row.get("content") or "").strip()
        if not content:
            return ""
        content = _truncate_to_tokens(content, _OVERVIEW_MAX_TOKENS)
        header = " Project Overview "
        parts = [
            f"{_BOX_H * 3}{header}{_BOX_H * (w - 3 - len(header))}",
            "",
        ]
        for line in content.splitlines():
            parts.append(f"  {line}")
        parts.append("")
        return "\n".join(parts)

    def _fetch_event_count(self, project_id: str) -> int:
        conn = self.repo.db.connect()
        try:
            row = conn.execute(
                "SELECT COUNT(*) as c FROM events WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            return row["c"] if row else 0
        finally:
            conn.close()

    def _build_extraction_warning(
        self, project_name: str, now_str: str, event_count: int,
    ) -> str:
        return (
            f"[{project_name}] recent context, {now_str}\n"
            + "\u2500" * 48 + "\n\n"
            f"\u26a0\ufe0f {event_count} events captured but 0 entities extracted.\n"
            f"   LLM backend may be unreachable. "
            f"Run `callmem doctor` to diagnose."
        )

    def _fetch_all_entities(
        self, project_id: str, focus: str | None,
    ) -> tuple[list[dict[str, Any]], int]:
        """Return (entities, suppressed_stale_count).

        Stale entities are excluded from the briefing by default. The
        caller surfaces the suppressed count as a footer so the agent
        knows memory was curated, not missing.
        """
        conn = self.repo.db.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM entities WHERE project_id = ? "
                "ORDER BY pinned DESC, created_at DESC LIMIT 200",
                (project_id,),
            ).fetchall()
            results = [dict(r) for r in rows]
        finally:
            conn.close()

        if focus:
            results = [
                r for r in results
                if focus.lower() in (r.get("title") or "").lower()
                or focus.lower() in (r.get("content") or "").lower()
            ]

        stale_count = sum(1 for r in results if r.get("stale"))
        results = [r for r in results if not r.get("stale")][:100]
        return results, stale_count

    def _fetch_sessions_with_entities(
        self, project_id: str
    ) -> list[dict[str, Any]]:
        conn = self.repo.db.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM sessions WHERE project_id = ? "
                "ORDER BY started_at DESC LIMIT 20",
                (project_id,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def _fetch_last_session(
        self, project_id: str
    ) -> dict[str, Any] | None:
        conn = self.repo.db.connect()
        try:
            row = conn.execute(
                "SELECT * FROM sessions "
                "WHERE project_id = ? AND status = 'ended' "
                "ORDER BY ended_at DESC LIMIT 1",
                (project_id,),
            ).fetchone()
            if row is None:
                return None
            return dict(row)
        finally:
            conn.close()

    def _compute_work_investment(self, project_id: str) -> int:
        conn = self.repo.db.connect()
        try:
            row = conn.execute(
                "SELECT COALESCE(SUM("
                "  COALESCE(token_count, LENGTH(content) / 4)"
                "), 0) as total FROM events WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            return row["total"] if row else 0
        finally:
            conn.close()

    def _append_latest_session_block(
        self,
        parts: list[str],
        last_session: dict[str, Any],
        sessions: list[dict[str, Any]],
        w: int,
    ) -> None:
        started = (last_session.get("started_at") or "")[:10]
        sid = last_session.get("id") or ""
        hook = _session_hook(last_session)
        header_label = f" Latest Session #{sid[:8]} ({started}) "
        pad = max(1, w - 3 - len(header_label))
        parts.append(f"{_BOX_H * 3}{header_label}{_BOX_H * pad}")
        parts.append("")

        if hook:
            parts.append(f"  {hook}")
            parts.append("")

        session_entities = self._fetch_entities_for_session(sid)

        investigated = [
            e for e in session_entities
            if e.get("type") in ("research", "discovery")
        ]
        learned = [
            e for e in session_entities
            if e.get("type") in ("fact", "decision")
        ]
        completed = [
            e for e in session_entities
            if e.get("type") in ("feature", "change")
            or (e.get("type") == "bugfix"
                and e.get("status") == "resolved")
        ]
        next_steps = [
            e for e in session_entities
            if e.get("type") in ("todo", "failure")
            and e.get("status") != "resolved"
        ]

        def _render_section(label: str, items: list[dict[str, Any]]) -> None:
            if not items:
                return
            parts.append(f"  {label}:")
            for e in items[:5]:
                eid = _short_id(e.get("id"))
                emoji = CATEGORY_EMOJI.get(e.get("type", ""), "")
                title = e.get("title") or ""
                parts.append(f"    #{eid}  {emoji}  {title}")
            if len(items) > 5:
                parts.append(f"    \u2026 ({len(items) - 5} more)")
            parts.append("")

        if not session_entities:
            summary = last_session.get("summary") or ""
            if summary and "\n" in summary:
                for line in summary.strip().split("\n")[1:]:
                    parts.append(f"  {line}")
                parts.append("")
            return

        _render_section("Investigated", investigated)
        _render_section("Learned", learned)
        _render_section("Completed", completed)
        _render_section("Next steps", next_steps)

    def _fetch_entities_for_session(
        self, session_id: str,
    ) -> list[dict[str, Any]]:
        if not session_id:
            return []
        conn = self.repo.db.connect()
        try:
            rows = conn.execute(
                "SELECT e.* FROM entities e "
                "JOIN events ev ON ev.id = e.source_event_id "
                "WHERE ev.session_id = ? AND e.stale = 0 "
                "ORDER BY e.pinned DESC, e.created_at DESC",
                (session_id,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def _group_entities_by_date(
        self, entities: list[dict[str, Any]]
    ) -> dict[str, list[dict[str, Any]]]:
        groups: dict[str, list[dict[str, Any]]] = {}
        for e in entities:
            ts = e.get("created_at") or ""
            date_str = ts[:10] if ts else "unknown"
            groups.setdefault(date_str, []).append(e)
        return dict(sorted(groups.items(), reverse=True))

    def _group_by_session(
        self,
        entities: list[dict[str, Any]],
        sessions: list[dict[str, Any]],
    ) -> list[tuple[str, list[dict[str, Any]]]]:
        session_map: dict[str, dict[str, Any]] = {
            s["id"]: s for s in sessions
        }

        ungrouped: list[dict[str, Any]] = []
        by_session: dict[str, list[dict[str, Any]]] = {}

        for e in entities:
            source_id = e.get("source_event_id")
            if not source_id:
                ungrouped.append(e)
                continue

            conn = self.repo.db.connect()
            try:
                ev_row = conn.execute(
                    "SELECT session_id FROM events WHERE id = ?",
                    (source_id,),
                ).fetchone()
            finally:
                conn.close()

            if ev_row and ev_row["session_id"]:
                sid = ev_row["session_id"]
                by_session.setdefault(sid, []).append(e)
            else:
                ungrouped.append(e)

        result: list[tuple[str, list[dict[str, Any]]]] = []

        for sid in sorted(
            by_session.keys(),
            key=lambda s: (session_map.get(s, {}).get("started_at") or ""),
            reverse=True,
        ):
            s_info = session_map.get(sid, {})
            started = (s_info.get("started_at") or "")[:10]
            status = s_info.get("status") or "unknown"
            hook = _session_hook(s_info)
            if hook:
                header = (
                    f"#S{sid[:8]} {hook} ({started}, {status})"
                )
            else:
                header = f"#S{sid[:8]} Session ({started}, {status})"
            result.append((header, by_session[sid]))

        if ungrouped:
            result.append(("Ungrouped", ungrouped))

        return result
