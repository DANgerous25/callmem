"""Startup briefing generator.

Assembles a compact context block from recent memories,
active TODOs, decisions, and summaries.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from llm_mem.core.retrieval import _estimate_tokens

if TYPE_CHECKING:
    from llm_mem.core.ollama import OllamaClient
    from llm_mem.core.repository import Repository
    from llm_mem.models.config import Config


@dataclass
class Briefing:
    """A formatted startup briefing."""

    project_name: str
    content: str
    token_count: int
    components: dict[str, int] = field(default_factory=dict)
    generated_at: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat()
    )


NEW_PROJECT_MESSAGE = (
    "## Session Briefing — {project_name}\n\n"
    "No prior memories found. This is a new project."
)

BRIEFING_TEMPLATE = """## Session Briefing — {project_name}

{todos_section}{decisions_section}{failures_section}{facts_section}{session_section}"""

TODO_SECTION = """### Active TODOs
{items}

"""

DECISION_SECTION = """### Recent Decisions
{items}

"""

FAILURE_SECTION = """### Unresolved Issues
{items}

"""

FACT_SECTION = """### Key Facts
{items}

"""

SESSION_SECTION = """### Last Session
{summary}

"""


def _truncate_to_tokens(text: str, max_tokens: int) -> str:
    """Truncate text to fit within token budget."""
    max_chars = max_tokens * 4
    if len(text) <= max_chars:
        return text
    return text[:max_chars - 3] + "..."


class BriefingGenerator:
    """Assembles a startup briefing from project memories."""

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
        """Generate a startup briefing for the given project."""
        budget = max_tokens or self.config.briefing.max_tokens
        components: dict[str, int] = {}

        todos = self._fetch_todos(project_id, focus)
        decisions = self._fetch_decisions(project_id, focus)
        failures = self._fetch_failures(project_id, focus)
        facts = self._fetch_facts(project_id, focus)
        last_session = self._fetch_last_session(project_id)

        if not any([todos, decisions, failures, facts, last_session]):
            content = NEW_PROJECT_MESSAGE.format(project_name=project_name)
            return Briefing(
                project_name=project_name,
                content=content,
                token_count=_estimate_tokens(content),
                components={"new_project": 1},
            )

        sections: list[tuple[str, str, int]] = []

        if todos:
            text = self._format_todos(todos)
            section = TODO_SECTION.format(items=text)
            sections.append(("todos", section, _estimate_tokens(section)))

        if decisions:
            text = self._format_decisions(decisions)
            section = DECISION_SECTION.format(items=text)
            sections.append(("decisions", section, _estimate_tokens(section)))

        if failures:
            text = self._format_failures(failures)
            section = FAILURE_SECTION.format(items=text)
            sections.append(("failures", section, _estimate_tokens(section)))

        if facts:
            text = self._format_facts(facts)
            section = FACT_SECTION.format(items=text)
            sections.append(("facts", section, _estimate_tokens(section)))

        if last_session:
            text = self._format_session(last_session)
            section = SESSION_SECTION.format(summary=text)
            sections.append(("last_session", section, _estimate_tokens(section)))

        header = f"## Session Briefing — {project_name}\n\n"
        remaining_budget = budget - _estimate_tokens(header)
        assembled = header

        for name, section, tokens in sections:
            if remaining_budget <= 0:
                break
            if tokens <= remaining_budget:
                assembled += section
                remaining_budget -= tokens
                components[name] = tokens
            else:
                truncated = _truncate_to_tokens(section, remaining_budget)
                assembled += truncated
                components[name] = _estimate_tokens(truncated)
                remaining_budget = 0

        return Briefing(
            project_name=project_name,
            content=assembled,
            token_count=_estimate_tokens(assembled),
            components=components,
        )

    def _fetch_todos(
        self, project_id: str, focus: str | None
    ) -> list[dict[str, Any]]:
        return self._fetch_entities(project_id, "todo", "open", focus)

    def _fetch_decisions(
        self, project_id: str, focus: str | None
    ) -> list[dict[str, Any]]:
        entities = self._fetch_entities(project_id, "decision", None, focus)
        return entities[:10]

    def _fetch_failures(
        self, project_id: str, focus: str | None
    ) -> list[dict[str, Any]]:
        return self._fetch_entities(
            project_id, "failure", "unresolved", focus
        )

    def _fetch_facts(
        self, project_id: str, focus: str | None
    ) -> list[dict[str, Any]]:
        conn = self.repo.db.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM entities WHERE project_id = ? AND type = 'fact' "
                "AND pinned = 1 "
                "ORDER BY updated_at DESC LIMIT 10",
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

    def _fetch_entities(
        self,
        project_id: str,
        entity_type: str,
        status: str | None,
        focus: str | None,
    ) -> list[dict[str, Any]]:
        conn = self.repo.db.connect()
        try:
            clauses: list[str] = [
                "project_id = ?", "type = ?"
            ]
            params: list[Any] = [project_id, entity_type]

            if status:
                clauses.append("status = ?")
                params.append(status)

            where = " AND ".join(clauses)
            rows = conn.execute(
                f"SELECT * FROM entities WHERE {where} "
                f"ORDER BY pinned DESC, updated_at DESC LIMIT 20",
                params,
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

    def _format_todos(self, todos: list[dict[str, Any]]) -> str:
        lines: list[str] = []
        for t in todos:
            priority = t.get("priority") or "medium"
            title = t.get("title") or ""
            content = (t.get("content") or "")[:80]
            lines.append(f"- [ ] {title} ({priority}) — {content}")
        return "\n".join(lines)

    def _format_decisions(self, decisions: list[dict[str, Any]]) -> str:
        lines: list[str] = []
        for d in decisions:
            title = d.get("title") or ""
            content = (d.get("content") or "")[:80]
            when = (d.get("updated_at") or "")[:10]
            lines.append(f"- {title}: {content} ({when})")
        return "\n".join(lines)

    def _format_failures(self, failures: list[dict[str, Any]]) -> str:
        lines: list[str] = []
        for f in failures:
            title = f.get("title") or ""
            content = (f.get("content") or "")[:80]
            lines.append(f"- {title}: {content}")
        return "\n".join(lines)

    def _format_facts(self, facts: list[dict[str, Any]]) -> str:
        lines: list[str] = []
        for f in facts:
            title = f.get("title") or ""
            content = (f.get("content") or "")[:80]
            lines.append(f"- {title}: {content}")
        return "\n".join(lines)

    def _format_session(self, session: dict[str, Any]) -> str:
        summary = session.get("summary") or "No summary recorded."
        event_count = session.get("event_count") or 0
        ended = (session.get("ended_at") or "unknown")[:10]
        return f"{summary} ({event_count} events, ended {ended})"
