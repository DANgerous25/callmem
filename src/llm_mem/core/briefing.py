"""Startup briefing generator.

Assembles a compact context block from recent memories,
active TODOs, decisions, and summaries with rich visual formatting.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

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
    "\u2500" * 48 + "\n\n"
    "No prior memories found. This is a new project."
)


def _truncate_to_tokens(text: str, max_tokens: int) -> str:
    max_chars = max_tokens * 4
    if len(text) <= max_chars:
        return text
    return text[:max_chars - 3] + "..."


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

        all_entities = self._fetch_all_entities(project_id, focus)
        sessions = self._fetch_sessions_with_entities(project_id)
        last_session = self._fetch_last_session(project_id)
        work_investment = self._compute_work_investment(project_id)

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

        now_str = datetime.now(UTC).strftime("%Y-%m-%d %-I:%M%p")
        parts.append(
            f"[{project_name}] recent context, {now_str}"
        )
        parts.append("\u2500" * 48)

        legend_parts = []
        for cat in LEGEND_ORDER:
            emoji = CATEGORY_EMOJI.get(cat, "")
            legend_parts.append(f"{emoji} {cat}")
        parts.append(f"Legend: {' | '.join(legend_parts)}")
        parts.append("")

        parts.append("Context Economics")
        parts.append(
            f"  Loading: {observations_loaded} observations "
            f"({read_tokens:,} tokens to read)"
        )
        parts.append(
            f"  Work investment: {work_investment:,} tokens "
            f"of captured work"
        )
        parts.append(
            f"  Your savings: {savings_pct}% reduction from reuse"
        )
        parts.append("")

        entities_by_date = self._group_entities_by_date(entities)
        for date_str, date_entities in entities_by_date.items():
            parts.append(f"{date_str}")
            parts.append("")

            by_session = self._group_by_session(date_entities, sessions)
            for session_header, session_entities in by_session:
                parts.append(f"  {session_header}")
                for e in session_entities:
                    emoji = CATEGORY_EMOJI.get(e.get("type", ""), "")
                    eid = (e.get("id") or "")[:8]
                    title = e.get("title") or ""
                    parts.append(f"    #{eid}  {emoji}  {title}")
                parts.append("")

        if last_session and last_session.get("summary"):
            parts.append("Latest Session Summary:")
            summary = last_session["summary"]
            for line in summary.split("\n"):
                parts.append(f"  {line}")
            parts.append("")

        footer = (
            f"Access {work_investment:,} tokens of past work "
            f"for just {read_tokens:,}t."
        )
        parts.append(footer)

        ui_port = self.config.ui.port
        ui_host = self.config.ui.host
        parts.append(f"View observations live @ http://{ui_host}:{ui_port}")

        return parts

    def _fetch_all_entities(
        self, project_id: str, focus: str | None
    ) -> list[dict[str, Any]]:
        conn = self.repo.db.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM entities WHERE project_id = ? "
                "ORDER BY pinned DESC, created_at DESC LIMIT 100",
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
        return results

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
            header = f"#S{sid[:8]} Session ({started}, {status})"
            result.append((header, by_session[sid]))

        if ungrouped:
            result.append(("Ungrouped", ungrouped))

        return result
