"""Startup briefing generator.

Assembles a compact context block from recent memories,
active TODOs, decisions, and summaries with rich visual formatting.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from llm_mem.compat import UTC
from llm_mem.core.retrieval import _estimate_tokens

if TYPE_CHECKING:
    from pathlib import Path

    from llm_mem.core.ollama import OllamaClient
    from llm_mem.core.repository import Repository
    from llm_mem.models.config import Config


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

        if not all_entities and not last_session:
            now_str = datetime.now(UTC).strftime("%Y-%m-%d %-I:%M%p")
            content = NEW_PROJECT_MESSAGE.format(
                project_name=project_name, datetime=now_str
            )
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

        if work_investment > 0 and read_tokens > 0:
            savings_pct = round(
                (1 - read_tokens / work_investment) * 100, 1
            )
        else:
            savings_pct = 0.0

        parts = self._build_briefing_parts(
            project_name, all_entities, sessions,
            last_session, observations_loaded, read_tokens,
            work_investment, savings_pct,
        )
        assembled = "\n".join(parts)

        if _estimate_tokens(assembled) > budget:
            assembled = _truncate_to_tokens(assembled, budget)

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

        # ── Context economics ──
        parts.append(f"{_BOX_H * 3} Context Economics {_BOX_H * (w - 22)}")
        parts.append("")
        parts.append(
            f"  {_BULLET} {observations_loaded} observations loaded "
            f"({read_tokens:,} tokens)"
        )
        parts.append(
            f"  {_BULLET} {work_investment:,} tokens of captured work"
        )
        if savings_pct > 0:
            parts.append(
                f"  {_BULLET} {savings_pct}% compression savings"
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

        # ── Footer ──
        parts.append(f"{_BOX_H * w}")
        parts.append(
            f"  {work_investment:,}t captured "
            f"{_BOX_V} {read_tokens:,}t to read "
            f"{_BOX_V} {savings_pct}% saved"
        )
        ui_port = self.config.ui.port
        ui_host = self.config.ui.host
        parts.append(
            f"  Web UI: http://{ui_host}:{ui_port}"
        )
        suppressed = getattr(self, "_suppressed_stale_count", 0) or 0
        if suppressed:
            parts.append(
                f"  ({suppressed} stale {'entity' if suppressed == 1 else 'entities'} "
                f"suppressed — run 'llm-mem stale' to review)"
            )

        return parts

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
