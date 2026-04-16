"""Data access layer — all SQL queries live here.

The repository pattern keeps SQL out of the engine and makes testing easier.
Every method takes model objects and returns model objects.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from llm_mem.models.events import Event
from llm_mem.models.projects import Project
from llm_mem.models.sessions import Session

if TYPE_CHECKING:
    from llm_mem.core.database import Database


class Repository:
    """Data access layer for llm-mem.

    All SQL is parameterized. All results are returned as model objects.
    """

    def __init__(self, db: Database) -> None:
        self.db = db

    # ── Projects ─────────────────────────────────────────────────────

    def create_project(self, project: Project) -> None:
        conn = self.db.connect()
        try:
            conn.execute(
                "INSERT INTO projects (id, name, root_path, created_at, updated_at, metadata) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    project.id,
                    project.name,
                    project.root_path,
                    project.created_at,
                    project.updated_at,
                    project.to_row()["metadata"],
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def get_project(self, project_id: str) -> Project | None:
        conn = self.db.connect()
        try:
            row = conn.execute(
                "SELECT * FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
            if row is None:
                return None
            return Project.from_row(dict(row))
        finally:
            conn.close()

    def get_project_by_name(self, name: str) -> Project | None:
        conn = self.db.connect()
        try:
            row = conn.execute(
                "SELECT * FROM projects WHERE name = ?", (name,)
            ).fetchone()
            if row is None:
                return None
            return Project.from_row(dict(row))
        finally:
            conn.close()

    # ── Sessions ─────────────────────────────────────────────────────

    def insert_session(self, session: Session) -> None:
        conn = self.db.connect()
        try:
            row = session.to_row()
            conn.execute(
                "INSERT INTO sessions "
                "(id, project_id, started_at, ended_at, status, agent_name, "
                "model_name, summary, event_count, metadata) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    row["id"], row["project_id"], row["started_at"],
                    row["ended_at"], row["status"], row["agent_name"],
                    row["model_name"], row["summary"], row["event_count"],
                    row["metadata"],
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def update_session(self, session: Session) -> None:
        conn = self.db.connect()
        try:
            row = session.to_row()
            conn.execute(
                "UPDATE sessions SET "
                "ended_at=?, status=?, agent_name=?, model_name=?, "
                "summary=?, event_count=?, metadata=? "
                "WHERE id=?",
                (
                    row["ended_at"], row["status"], row["agent_name"],
                    row["model_name"], row["summary"], row["event_count"],
                    row["metadata"], row["id"],
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def get_session(self, session_id: str) -> Session | None:
        conn = self.db.connect()
        try:
            row = conn.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if row is None:
                return None
            return Session.from_row(dict(row))
        finally:
            conn.close()

    def get_active_session(self, project_id: str) -> Session | None:
        conn = self.db.connect()
        try:
            row = conn.execute(
                "SELECT * FROM sessions "
                "WHERE project_id = ? AND status = 'active' "
                "ORDER BY started_at DESC LIMIT 1",
                (project_id,),
            ).fetchone()
            if row is None:
                return None
            return Session.from_row(dict(row))
        finally:
            conn.close()

    def list_sessions(
        self, project_id: str, limit: int = 20, offset: int = 0
    ) -> list[Session]:
        conn = self.db.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM sessions WHERE project_id = ? "
                "ORDER BY started_at DESC LIMIT ? OFFSET ?",
                (project_id, limit, offset),
            ).fetchall()
            return [Session.from_row(dict(r)) for r in rows]
        finally:
            conn.close()

    # ── Events ───────────────────────────────────────────────────────

    def insert_event(self, event: Event) -> None:
        conn = self.db.connect()
        try:
            row = event.to_row()
            conn.execute(
                "INSERT INTO events "
                "(id, session_id, project_id, type, content, timestamp, "
                "token_count, metadata, archived_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    row["id"], row["session_id"], row["project_id"],
                    row["type"], row["content"], row["timestamp"],
                    row["token_count"], row["metadata"], row["archived_at"],
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def insert_events(self, events: list[Event]) -> None:
        if not events:
            return
        conn = self.db.connect()
        try:
            rows = [e.to_row() for e in events]
            conn.executemany(
                "INSERT INTO events "
                "(id, session_id, project_id, type, content, timestamp, "
                "token_count, metadata, archived_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        r["id"], r["session_id"], r["project_id"],
                        r["type"], r["content"], r["timestamp"],
                        r["token_count"], r["metadata"], r["archived_at"],
                    )
                    for r in rows
                ],
            )
            conn.commit()
        finally:
            conn.close()

    def get_event(self, event_id: str) -> Event | None:
        conn = self.db.connect()
        try:
            row = conn.execute(
                "SELECT * FROM events WHERE id = ?", (event_id,)
            ).fetchone()
            if row is None:
                return None
            return Event.from_row(dict(row))
        finally:
            conn.close()

    def get_events(
        self,
        project_id: str,
        session_id: str | None = None,
        type: str | None = None,
        limit: int = 50,
    ) -> list[Event]:
        conn = self.db.connect()
        try:
            clauses: list[str] = ["project_id = ?"]
            params: list[Any] = [project_id]

            if session_id is not None:
                clauses.append("session_id = ?")
                params.append(session_id)
            if type is not None:
                clauses.append("type = ?")
                params.append(type)

            where = " AND ".join(clauses)
            params.append(limit)

            rows = conn.execute(
                f"SELECT * FROM events WHERE {where} "
                f"ORDER BY timestamp DESC LIMIT ?",
                params,
            ).fetchall()
            return [Event.from_row(dict(r)) for r in rows]
        finally:
            conn.close()

    def count_events(
        self, project_id: str, session_id: str | None = None
    ) -> int:
        conn = self.db.connect()
        try:
            clauses: list[str] = ["project_id = ?"]
            params: list[Any] = [project_id]

            if session_id is not None:
                clauses.append("session_id = ?")
                params.append(session_id)

            where = " AND ".join(clauses)
            row = conn.execute(
                f"SELECT COUNT(*) as c FROM events WHERE {where}", params
            ).fetchone()
            return row["c"]
        finally:
            conn.close()

    def find_recent_event(
        self,
        project_id: str,
        content: str,
        type: str,
        within_seconds: int,
    ) -> Event | None:
        conn = self.db.connect()
        try:
            row = conn.execute(
                "SELECT * FROM events "
                "WHERE project_id = ? AND content = ? AND type = ? "
                "AND timestamp >= datetime('now', ?) "
                "ORDER BY timestamp DESC LIMIT 1",
                (project_id, content, type, f"-{within_seconds} seconds"),
            ).fetchone()
            if row is None:
                return None
            return Event.from_row(dict(row))
        finally:
            conn.close()
