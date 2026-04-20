"""Data access layer — all SQL queries live here.

The repository pattern keeps SQL out of the engine and makes testing easier.
Every method takes model objects and returns model objects.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from callmem.models.events import Event
from callmem.models.projects import Project
from callmem.models.sessions import Session

if TYPE_CHECKING:
    from callmem.core.database import Database


class Repository:
    """Data access layer for callmem.

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

    # ── Entities ─────────────────────────────────────────────────────

    def get_entities(
        self,
        project_id: str,
        type: str | None = None,
        status: str | None = None,
        limit: int = 50,
        include_stale: bool = False,
    ) -> list[dict[str, Any]]:
        from callmem.models.entities import Entity

        conn = self.db.connect()
        try:
            clauses: list[str] = ["project_id = ?"]
            params: list[Any] = [project_id]
            if type is not None:
                clauses.append("type = ?")
                params.append(type)
            if status is not None:
                clauses.append("status = ?")
                params.append(status)
            if not include_stale:
                clauses.append("stale = 0")

            where = " AND ".join(clauses)
            params.append(limit)

            rows = conn.execute(
                f"SELECT * FROM entities WHERE {where} "
                f"ORDER BY pinned DESC, updated_at DESC LIMIT ?",
                params,
            ).fetchall()
            return [dict(Entity.from_row(dict(r)).to_row()) for r in rows]
        finally:
            conn.close()

    def mark_stale(
        self,
        entity_id: str,
        reason: str,
        superseded_by: str | None = None,
    ) -> bool:
        """Flag an entity as stale. Returns True if a row was modified."""
        conn = self.db.connect()
        try:
            cursor = conn.execute(
                "UPDATE entities "
                "SET stale = 1, staleness_reason = ?, superseded_by = ?, "
                "    updated_at = datetime('now') "
                "WHERE id = ? AND stale = 0",
                (reason, superseded_by, entity_id),
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()

    def mark_current(self, entity_id: str) -> bool:
        """Clear the stale flag on an entity. Returns True if modified."""
        conn = self.db.connect()
        try:
            cursor = conn.execute(
                "UPDATE entities "
                "SET stale = 0, staleness_reason = NULL, superseded_by = NULL, "
                "    updated_at = datetime('now') "
                "WHERE id = ? AND stale = 1",
                (entity_id,),
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()

    def list_stale_entities(
        self, project_id: str, limit: int = 200,
    ) -> list[dict[str, Any]]:
        from callmem.models.entities import Entity

        conn = self.db.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM entities "
                "WHERE project_id = ? AND stale = 1 "
                "ORDER BY updated_at DESC LIMIT ?",
                (project_id, limit),
            ).fetchall()
            return [dict(Entity.from_row(dict(r)).to_row()) for r in rows]
        finally:
            conn.close()

    def get_entity(self, entity_id: str) -> dict[str, Any] | None:
        from callmem.models.entities import Entity

        conn = self.db.connect()
        try:
            row = conn.execute(
                "SELECT * FROM entities WHERE id = ?", (entity_id,),
            ).fetchone()
            if row is None:
                return None
            return dict(Entity.from_row(dict(row)).to_row())
        finally:
            conn.close()

    def set_pinned(self, entity_id: str, pinned: bool) -> dict[str, Any]:
        from callmem.models.entities import Entity

        conn = self.db.connect()
        try:
            entity_row = conn.execute(
                "SELECT * FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
            if entity_row is None:
                msg = f"Entity not found: {entity_id}"
                raise ValueError(msg)

            conn.execute(
                "UPDATE entities SET pinned = ?, updated_at = datetime('now') "
                "WHERE id = ?",
                (1 if pinned else 0, entity_id),
            )
            conn.commit()

            updated = conn.execute(
                "SELECT * FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
            entity = Entity.from_row(dict(updated))
            return dict(entity.to_row())
        finally:
            conn.close()

    def resolve_entity(self, entity_id: str, status: str) -> bool:
        """Update an entity's status. Returns True if updated."""
        conn = self.db.connect()
        try:
            cursor = conn.execute(
                "UPDATE entities SET status = ?, updated_at = datetime('now') "
                "WHERE id = ? AND status != ?",
                (status, entity_id, status),
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()

    def find_open_entities_by_keywords(
        self,
        project_id: str,
        entity_types: list[str],
        statuses: list[str],
        keywords: list[str],
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Find open entities whose titles contain enough of the given keywords."""
        conn = self.db.connect()
        try:
            type_placeholders = ",".join("?" for _ in entity_types)
            status_placeholders = ",".join("?" for _ in statuses)
            rows = conn.execute(
                f"SELECT id, type, title, status FROM entities "
                f"WHERE project_id = ? "
                f"AND type IN ({type_placeholders}) "
                f"AND status IN ({status_placeholders}) "
                f"ORDER BY created_at DESC LIMIT 200",
                [project_id, *entity_types, *statuses],
            ).fetchall()

            scored: list[tuple[int, dict[str, Any]]] = []
            kw_lower = [k.lower() for k in keywords if len(k) > 3]
            if not kw_lower:
                return []

            for r in rows:
                title_lower = r["title"].lower()
                match_count = sum(1 for k in kw_lower if k in title_lower)
                threshold = max(2, len(kw_lower) // 2)
                if match_count >= threshold:
                    scored.append((match_count, dict(r)))

            scored.sort(key=lambda x: -x[0])
            return [item[1] for item in scored[:limit]]
        finally:
            conn.close()

    # ── Vault ─────────────────────────────────────────────────────────

    def insert_vault_entry(
        self,
        id: str,
        project_id: str,
        category: str,
        detector: str,
        pattern_name: str | None,
        ciphertext: bytes,
        event_id: str,
    ) -> None:
        conn = self.db.connect()
        try:
            conn.execute(
                "INSERT INTO vault "
                "(id, project_id, category, detector, pattern_name, "
                "ciphertext, created_at, event_id) "
                "VALUES (?, ?, ?, ?, ?, ?, datetime('now'), ?)",
                (id, project_id, category, detector, pattern_name, ciphertext, event_id),
            )
            conn.commit()
        finally:
            conn.close()

    def get_vault_entry(self, vault_id: str) -> dict[str, Any] | None:
        conn = self.db.connect()
        try:
            row = conn.execute(
                "SELECT * FROM vault WHERE id = ?", (vault_id,)
            ).fetchone()
            if row is None:
                return None
            return dict(row)
        finally:
            conn.close()

    def mark_vault_false_positive(self, vault_id: str) -> None:
        conn = self.db.connect()
        try:
            conn.execute(
                "UPDATE vault SET false_positive = 1 WHERE id = ?",
                (vault_id,),
            )
            conn.commit()
        finally:
            conn.close()

    def update_event_content(self, event_id: str, content: str) -> None:
        conn = self.db.connect()
        try:
            conn.execute(
                "UPDATE events SET content = ? WHERE id = ?",
                (content, event_id),
            )
            conn.commit()
        finally:
            conn.close()

    def get_entities_by_file(
        self, file_path: str, limit: int = 20
    ) -> list[dict[str, Any]]:
        from callmem.models.entities import Entity

        conn = self.db.connect()
        try:
            rows = conn.execute(
                "SELECT e.* FROM entities e "
                "JOIN entity_files ef ON e.id = ef.entity_id "
                "WHERE ef.file_path = ? "
                "ORDER BY e.created_at DESC LIMIT ?",
                (file_path, limit),
            ).fetchall()
            return [dict(Entity.from_row(dict(r)).to_row()) for r in rows]
        finally:
            conn.close()

    def get_files_for_entity(
        self, entity_id: str
    ) -> list[dict[str, Any]]:
        conn = self.db.connect()
        try:
            rows = conn.execute(
                "SELECT file_path, relation FROM entity_files "
                "WHERE entity_id = ?",
                (entity_id,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def list_files_with_observations(
        self, project_id: str, limit: int = 200,
    ) -> list[dict[str, Any]]:
        """List tracked files for a project with observation counts.

        Only counts live entities (not stale, not archived). Ordered by
        most-recently observed first.
        """
        conn = self.db.connect()
        try:
            rows = conn.execute(
                "SELECT ef.file_path AS file_path, "
                "COUNT(*) AS observation_count, "
                "MAX(e.created_at) AS last_modified "
                "FROM entity_files ef "
                "JOIN entities e ON e.id = ef.entity_id "
                "WHERE e.project_id = ? "
                "AND COALESCE(e.stale, 0) = 0 "
                "AND e.archived_at IS NULL "
                "GROUP BY ef.file_path "
                "ORDER BY last_modified DESC "
                "LIMIT ?",
                (project_id, limit),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_file_timeline(
        self, file_path: str, limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Return entities linked to a file, oldest-first, excluding stale.

        Tries an exact match first, then falls back to any path whose
        basename matches (covers `./foo.py` vs `src/foo.py` variants).
        """
        import os

        normalized = file_path.lstrip("./") or file_path
        conn = self.db.connect()
        try:
            rows = conn.execute(
                "SELECT e.*, ef.file_path AS matched_path "
                "FROM entities e "
                "JOIN entity_files ef ON e.id = ef.entity_id "
                "WHERE (ef.file_path = ? OR ef.file_path = ?) "
                "AND COALESCE(e.stale, 0) = 0 "
                "AND e.archived_at IS NULL "
                "ORDER BY e.created_at ASC LIMIT ?",
                (file_path, normalized, limit),
            ).fetchall()
            if not rows:
                basename = os.path.basename(normalized)
                if basename:
                    rows = conn.execute(
                        "SELECT e.*, ef.file_path AS matched_path "
                        "FROM entities e "
                        "JOIN entity_files ef ON e.id = ef.entity_id "
                        "WHERE (ef.file_path = ? OR ef.file_path LIKE ?) "
                        "AND COALESCE(e.stale, 0) = 0 "
                        "AND e.archived_at IS NULL "
                        "ORDER BY e.created_at ASC LIMIT ?",
                        (basename, f"%/{basename}", limit),
                    ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_timeline(
        self,
        project_id: str,
        anchor_id: str | None = None,
        depth_before: int = 3,
        depth_after: int = 3,
    ) -> list[dict[str, Any]]:
        from callmem.models.entities import Entity

        conn = self.db.connect()
        try:
            if anchor_id:
                anchor = conn.execute(
                    "SELECT * FROM entities WHERE id = ?",
                    (anchor_id,),
                ).fetchone()
                if anchor is None:
                    return []
                anchor_time = anchor["created_at"]
                before = conn.execute(
                    "SELECT * FROM entities WHERE project_id = ? "
                    "AND created_at < ? "
                    "ORDER BY created_at DESC LIMIT ?",
                    (project_id, anchor_time, depth_before),
                ).fetchall()
                after = conn.execute(
                    "SELECT * FROM entities WHERE project_id = ? "
                    "AND created_at > ? "
                    "ORDER BY created_at ASC LIMIT ?",
                    (project_id, anchor_time, depth_after),
                ).fetchall()
                all_rows = list(reversed(before)) + [anchor] + after
            else:
                all_rows = conn.execute(
                    "SELECT * FROM entities WHERE project_id = ? "
                    "ORDER BY created_at DESC LIMIT ?",
                    (project_id, depth_before + depth_after + 1),
                ).fetchall()

            return [
                dict(Entity.from_row(dict(r)).to_row()) for r in all_rows
            ]
        finally:
            conn.close()
