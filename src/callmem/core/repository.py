"""Data access layer — all SQL queries live here.

The repository pattern keeps SQL out of the engine and makes testing easier.
Every method takes model objects and returns model objects.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from callmem.models.events import Event
from callmem.models.model_registry import ModelRegistryEntry
from callmem.models.projects import Project
from callmem.models.rewind import RewindPoint
from callmem.models.sessions import Session
from callmem.models.tasks import Task

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

    def list_projects(self) -> list[dict[str, Any]]:
        """List all projects with id and name."""
        conn = self.db.connect()
        try:
            rows = conn.execute("SELECT id, name FROM projects ORDER BY name").fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # ── Project overview ─────────────────────────────────────────────

    def set_overview(
        self,
        project_id: str,
        content: str,
        updated_by: str | None = None,
    ) -> dict[str, Any]:
        """Upsert the project overview. One row per project.

        Returns the stored row as a dict.
        """
        from datetime import datetime

        from callmem.compat import UTC

        now = datetime.now(UTC).isoformat()
        conn = self.db.connect()
        try:
            conn.execute(
                "INSERT INTO project_overview (project_id, content, updated_at, updated_by) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(project_id) DO UPDATE SET "
                "  content = excluded.content, "
                "  updated_at = excluded.updated_at, "
                "  updated_by = excluded.updated_by",
                (project_id, content, now, updated_by),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM project_overview WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            return dict(row) if row else {
                "project_id": project_id,
                "content": content,
                "updated_at": now,
                "updated_by": updated_by,
            }
        finally:
            conn.close()

    def get_overview(self, project_id: str) -> dict[str, Any] | None:
        """Return the project overview row, or None if not set."""
        conn = self.db.connect()
        try:
            row = conn.execute(
                "SELECT * FROM project_overview WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            return dict(row) if row else None
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

    def get_sessions_by_ids(
        self, session_ids: list[str]
    ) -> list[dict[str, Any]]:
        """Fetch sessions by IDs, returning raw dicts."""
        if not session_ids:
            return []
        conn = self.db.connect()
        try:
            placeholders = ",".join("?" for _ in session_ids)
            rows = conn.execute(
                f"SELECT * FROM sessions WHERE id IN ({placeholders})",
                session_ids,
            ).fetchall()
            return [dict(r) for r in rows]
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

    def get_events_by_ids(
        self, event_ids: list[str], order_by: str = "timestamp ASC"
    ) -> list[dict[str, Any]]:
        """Fetch events by their IDs. Returns raw dicts."""
        if not event_ids:
            return []
        conn = self.db.connect()
        try:
            placeholders = ",".join("?" for _ in event_ids)
            rows = conn.execute(
                f"SELECT * FROM events WHERE id IN ({placeholders}) "
                f"ORDER BY {order_by}",
                event_ids,
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def count_all(
        self, table: str, project_id: str | None = None,
    ) -> int:
        """Count rows in a table, optionally filtered by project_id."""
        conn = self.db.connect()
        try:
            if project_id:
                row = conn.execute(
                    f"SELECT COUNT(*) as c FROM {table} WHERE project_id = ?",
                    (project_id,),
                ).fetchone()
            else:
                row = conn.execute(
                    f"SELECT COUNT(*) as c FROM {table}",
                ).fetchone()
            return row["c"]
        finally:
            conn.close()

    def get_session_event_ids_for_summary(
        self, session_id: str, chunk_size: int, offset: int,
    ) -> list[str]:
        """Get event IDs for a chunk summary at the given offset."""
        conn = self.db.connect()
        try:
            rows = conn.execute(
                "SELECT id FROM events "
                "WHERE session_id = ? ORDER BY timestamp ASC "
                "LIMIT ? OFFSET ?",
                (session_id, chunk_size, offset),
            ).fetchall()
            return [r["id"] for r in rows]
        finally:
            conn.close()

    def count_ended_sessions(self, project_id: str) -> int:
        """Count ended sessions for a project."""
        conn = self.db.connect()
        try:
            row = conn.execute(
                "SELECT COUNT(*) as c FROM sessions "
                "WHERE project_id = ? AND status = 'ended'",
                (project_id,),
            ).fetchone()
            return row["c"]
        finally:
            conn.close()

    def insert_summary(self, id: str, project_id: str, session_id: str | None,
                       level: str, content: str,
                       event_range_start: str | None,
                       event_range_end: str | None,
                       event_count: int | None,
                       token_count: int,
                       created_at: str,
                       metadata: str | None) -> None:
        conn = self.db.connect()
        try:
            conn.execute(
                "INSERT INTO summaries "
                "(id, project_id, session_id, level, content, "
                "event_range_start, event_range_end, event_count, "
                "token_count, created_at, metadata) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (id, project_id, session_id, level, content,
                 event_range_start, event_range_end, event_count,
                 token_count, created_at, metadata),
            )
            conn.commit()
        finally:
            conn.close()

    # ── FTS5 ─────────────────────────────────────────────────────────

    def search_events_fts(
        self, project_id: str, query: str, limit: int = 20
    ) -> list[dict[str, Any]]:
        """Search events using FTS5, filtered by project."""
        conn = self.db.connect()
        try:
            rows = conn.execute(
                "SELECT e.id, e.type, e.content, e.timestamp, e.session_id, "
                "e.archived_at "
                "FROM events_fts f "
                "JOIN events e ON e.rowid = f.rowid "
                "WHERE events_fts MATCH ? AND e.project_id = ? "
                "ORDER BY rank LIMIT ?",
                (query, project_id, limit),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def search_entities_fts_by_type(
        self, project_id: str, query: str, entity_type: str,
        before: str, exclude_id: str, limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Search entities using FTS5, filtered by project and type."""
        conn = self.db.connect()
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
                (query, project_id, entity_type, before, exclude_id, limit),
            ).fetchall()
            return [dict(r) for r in rows]
        except Exception:  # pragma: no cover — bad FTS query
            return []
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

    def create_entity(self, entity: Entity) -> None:
        """Insert a new entity directly (no LLM extraction)."""
        from callmem.models.entities import Entity as _Entity

        conn = self.db.connect()
        try:
            row = entity.to_row()
            conn.execute(
                "INSERT INTO entities "
                "(id, project_id, source_event_id, type, title, content, "
                "key_points, synopsis, extracted_by, "
                "status, priority, pinned, created_at, updated_at, "
                "resolved_at, metadata, archived_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    row["id"], row["project_id"], row["source_event_id"],
                    row["type"], row["title"], row["content"],
                    row["key_points"], row["synopsis"], row.get("extracted_by"),
                    row["status"], row["priority"], row["pinned"],
                    row["created_at"], row["updated_at"],
                    row["resolved_at"], row["metadata"], row["archived_at"],
                ),
            )
            conn.commit()
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

    def get_entity_by_short_id(self, short_id: str) -> dict[str, Any] | None:
        """Look up an entity by its short ID prefix or suffix."""
        from callmem.models.entities import Entity

        conn = self.db.connect()
        try:
            row = conn.execute(
                "SELECT * FROM entities "
                "WHERE id LIKE ? OR id LIKE ? "
                "LIMIT 1",
                (f"{short_id}%", f"%{short_id}"),
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

    # ── Tasks (A1) ───────────────────────────────────────────────────

    def insert_task(self, task: Task) -> None:
        conn = self.db.connect()
        try:
            row = task.to_row()
            conn.execute(
                "INSERT INTO tasks "
                "(id, project_id, parent_id, session_id, title, description, "
                "status, model_assigned, model_reason, eval_score, "
                "eval_feedback, cost_usd, tokens_input, tokens_output, "
                "result_ref, task_type, complexity_hint, retry_count, "
                "retry_of, created_at, updated_at, completed_at, metadata) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                "?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    row["id"], row["project_id"], row["parent_id"],
                    row["session_id"], row["title"], row["description"],
                    row["status"], row["model_assigned"], row["model_reason"],
                    row["eval_score"], row["eval_feedback"],
                    row["cost_usd"], row["tokens_input"], row["tokens_output"],
                    row["result_ref"], row["task_type"], row["complexity_hint"],
                    row["retry_count"], row["retry_of"],
                    row["created_at"], row["updated_at"],
                    row["completed_at"], row["metadata"],
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def get_task(self, task_id: str) -> Task | None:
        conn = self.db.connect()
        try:
            row = conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if row is None:
                return None
            return Task.from_row(dict(row))
        finally:
            conn.close()

    def update_task(self, task_id: str, fields: dict[str, Any]) -> bool:
        """Update specific fields on a task. Returns True if a row was modified."""
        conn = self.db.connect()
        try:
            allowed = {
                "status", "model_assigned", "model_reason", "eval_score",
                "eval_feedback", "cost_usd", "tokens_input", "tokens_output",
                "result_ref", "task_type", "complexity_hint", "retry_count",
                "completed_at", "description", "title",
            }
            updates = {k: v for k, v in fields.items() if k in allowed}
            if not updates:
                return False

            set_clauses = ", ".join(f"{k} = ?" for k in updates)
            values = list(updates.values())
            values.append(task_id)

            cursor = conn.execute(
                f"UPDATE tasks SET {set_clauses}, "
                f"updated_at = datetime('now') "
                f"WHERE id = ?",
                values,
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()

    def list_tasks(
        self,
        project_id: str,
        status: str | None = None,
        parent_id: str | None = None,
        session_id: str | None = None,
        task_type: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        conn = self.db.connect()
        try:
            clauses: list[str] = ["project_id = ?"]
            params: list[Any] = [project_id]
            if status is not None:
                clauses.append("status = ?")
                params.append(status)
            if parent_id is not None:
                clauses.append("parent_id IS ?")
                params.append(parent_id)
            if session_id is not None:
                clauses.append("session_id = ?")
                params.append(session_id)
            if task_type is not None:
                clauses.append("task_type = ?")
                params.append(task_type)

            where = " AND ".join(clauses)
            params.append(limit)
            rows = conn.execute(
                f"SELECT * FROM tasks WHERE {where} "
                f"ORDER BY created_at ASC LIMIT ?",
                params,
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_task_tree(self, root_id: str) -> list[dict[str, Any]]:
        """Recursively fetch a task and all its descendants."""
        conn = self.db.connect()
        try:
            rows = conn.execute(
                "WITH RECURSIVE tree AS ("
                "  SELECT * FROM tasks WHERE id = ?"
                "  UNION ALL"
                "  SELECT t.* FROM tasks t JOIN tree ON t.parent_id = tree.id"
                ") SELECT * FROM tree ORDER BY created_at ASC",
                (root_id,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # ── Model Stats (A2) ─────────────────────────────────────────────

    def upsert_model_stats(
        self,
        project_id: str,
        model_name: str,
        task_type: str | None = None,
        *,
        completed_delta: int = 0,
        failed_delta: int = 0,
        eval_score: float | None = None,
        cost_delta: float = 0.0,
        tokens_in_delta: int = 0,
        tokens_out_delta: int = 0,
    ) -> None:
        """Incrementally upsert model performance stats."""
        from datetime import datetime

        from ulid import ULID

        from callmem.compat import UTC

        conn = self.db.connect()
        try:
            now = datetime.now(UTC).isoformat()
            tt = task_type or "_overall"
            existing = conn.execute(
                "SELECT * FROM model_stats "
                "WHERE project_id = ? AND model_name = ? AND "
                "(task_type = ? OR (task_type IS NULL AND ? = '_overall'))",
                (project_id, model_name, tt, tt),
            ).fetchone()

            if existing is None:
                new_id = str(ULID())
                conn.execute(
                    "INSERT INTO model_stats "
                    "(id, project_id, model_name, task_type, "
                    "tasks_completed, tasks_failed, avg_eval_score, "
                    "total_cost_usd, total_tokens_in, total_tokens_out, "
                    "first_seen, last_seen, metadata) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        new_id, project_id, model_name,
                        task_type if task_type else None,
                        completed_delta, failed_delta, eval_score,
                        cost_delta, tokens_in_delta, tokens_out_delta,
                        now, now, None,
                    ),
                )
            else:
                new_completed = existing["tasks_completed"] + completed_delta
                new_failed = existing["tasks_failed"] + failed_delta
                new_cost = existing["total_cost_usd"] + cost_delta
                new_tokens_in = existing["total_tokens_in"] + tokens_in_delta
                new_tokens_out = existing["total_tokens_out"] + tokens_out_delta

                if eval_score is not None:
                    total = new_completed + new_failed
                    if total > 0:
                        old_avg = existing["avg_eval_score"] or 0.0
                        old_count = max(0, total - 1)
                        new_avg = (
                            (old_avg * old_count + eval_score) / total
                        )
                    else:
                        new_avg = eval_score
                else:
                    new_avg = existing["avg_eval_score"]

                conn.execute(
                    "UPDATE model_stats SET "
                    "tasks_completed = ?, tasks_failed = ?, "
                    "avg_eval_score = ?, total_cost_usd = ?, "
                    "total_tokens_in = ?, total_tokens_out = ?, "
                    "last_seen = ? "
                    "WHERE id = ?",
                    (
                        new_completed, new_failed, new_avg,
                        new_cost, new_tokens_in, new_tokens_out,
                        now, existing["id"],
                    ),
                )
            conn.commit()
        finally:
            conn.close()

    def get_model_stats(
        self, project_id: str, model_name: str,
        task_type: str | None = None,
    ) -> dict[str, Any] | None:
        conn = self.db.connect()
        try:
            if task_type:
                row = conn.execute(
                    "SELECT * FROM model_stats "
                    "WHERE project_id = ? AND model_name = ? AND task_type = ?",
                    (project_id, model_name, task_type),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM model_stats "
                    "WHERE project_id = ? AND model_name = ? "
                    "AND task_type IS NULL",
                    (project_id, model_name),
                ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def list_model_stats(
        self, project_id: str,
        model_name: str | None = None,
        task_type: str | None = None,
    ) -> list[dict[str, Any]]:
        conn = self.db.connect()
        try:
            clauses: list[str] = ["project_id = ?"]
            params: list[Any] = [project_id]
            if model_name is not None:
                clauses.append("model_name = ?")
                params.append(model_name)
            if task_type is not None:
                clauses.append("task_type = ?")
                params.append(task_type)
            where = " AND ".join(clauses)
            rows = conn.execute(
                f"SELECT * FROM model_stats WHERE {where} "
                f"ORDER BY last_seen DESC",
                params,
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # ── Eval (A3) ─────────────────────────────────────────────────────

    def update_event_eval(
        self, event_id: str, eval_score: float,
        eval_feedback: str | None = None,
        eval_model: str | None = None,
    ) -> bool:
        conn = self.db.connect()
        try:
            cursor = conn.execute(
                "UPDATE events SET "
                "eval_score = ?, eval_feedback = ?, eval_model = ? "
                "WHERE id = ?",
                (eval_score, eval_feedback, eval_model, event_id),
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()

    def update_entity_eval(
        self, entity_id: str, eval_score: float,
        eval_feedback: str | None = None,
    ) -> bool:
        conn = self.db.connect()
        try:
            cursor = conn.execute(
                "UPDATE entities SET "
                "eval_score = ?, eval_feedback = ?, "
                "updated_at = datetime('now') "
                "WHERE id = ?",
                (eval_score, eval_feedback, entity_id),
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()

    def get_eval_summary(
        self, project_id: str,
        entity_type: str | None = None,
        model_name: str | None = None,
    ) -> dict[str, Any]:
        """Aggregate eval scores across events and entities."""
        conn = self.db.connect()
        try:
            results: dict[str, Any] = {}

            event_clauses = ["e.project_id = ?", "e.eval_score IS NOT NULL"]
            event_params: list[Any] = [project_id]
            if model_name is not None:
                event_clauses.append("e.eval_model = ?")
                event_params.append(model_name)

            event_where = " AND ".join(event_clauses)
            row = conn.execute(
                f"SELECT COUNT(*) as count, AVG(e.eval_score) as avg_score "
                f"FROM events e WHERE {event_where}",
                event_params,
            ).fetchone()
            results["events"] = {
                "count": row["count"] if row else 0,
                "avg_score": round(row["avg_score"], 4) if row and row["avg_score"] else None,
            }

            entity_clauses = ["project_id = ?", "eval_score IS NOT NULL"]
            entity_params: list[Any] = [project_id]
            if entity_type is not None:
                entity_clauses.append("type = ?")
                entity_params.append(entity_type)

            entity_where = " AND ".join(entity_clauses)
            row = conn.execute(
                f"SELECT COUNT(*) as count, AVG(eval_score) as avg_score "
                f"FROM entities WHERE {entity_where}",
                entity_params,
            ).fetchone()
            results["entities"] = {
                "count": row["count"] if row else 0,
                "avg_score": round(row["avg_score"], 4) if row and row["avg_score"] else None,
            }

            return results
        finally:
            conn.close()

    # ── Model Registry (A5) ───────────────────────────────────────────

    def upsert_model_registry(self, entry: ModelRegistryEntry) -> None:
        conn = self.db.connect()
        try:
            row = entry.to_row()
            conn.execute(
                "INSERT INTO model_registry "
                "(model_name, provider, display_name, pricing_input, "
                "pricing_output, context_window, max_output, "
                "supports_tools, supports_vision, supports_streaming, "
                "strengths, weaknesses, benchmarks, latency_p50_ms, "
                "geo_available, geo_blocked, geo_notes, quality_tier, "
                "use_case_scores, known_issues, release_date, "
                "deprecation_date, gateways, last_synced, "
                "last_researched, last_updated, metadata) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                "?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(model_name) DO UPDATE SET "
                "provider=excluded.provider, display_name=excluded.display_name, "
                "pricing_input=excluded.pricing_input, pricing_output=excluded.pricing_output, "
                "context_window=excluded.context_window, max_output=excluded.max_output, "
                "supports_tools=excluded.supports_tools, supports_vision=excluded.supports_vision, "
                "supports_streaming=excluded.supports_streaming, "
                "strengths=excluded.strengths, weaknesses=excluded.weaknesses, "
                "benchmarks=excluded.benchmarks, latency_p50_ms=excluded.latency_p50_ms, "
                "geo_available=excluded.geo_available, geo_blocked=excluded.geo_blocked, "
                "geo_notes=excluded.geo_notes, quality_tier=excluded.quality_tier, "
                "use_case_scores=excluded.use_case_scores, known_issues=excluded.known_issues, "
                "release_date=excluded.release_date, deprecation_date=excluded.deprecation_date, "
                "gateways=excluded.gateways, last_synced=excluded.last_synced, "
                "last_researched=excluded.last_researched, last_updated=excluded.last_updated, "
                "metadata=excluded.metadata",
                (
                    row["model_name"], row["provider"], row["display_name"],
                    row["pricing_input"], row["pricing_output"],
                    row["context_window"], row["max_output"],
                    row["supports_tools"], row["supports_vision"],
                    row["supports_streaming"],
                    row["strengths"], row["weaknesses"], row["benchmarks"],
                    row["latency_p50_ms"],
                    row["geo_available"], row["geo_blocked"], row["geo_notes"],
                    row["quality_tier"], row["use_case_scores"],
                    row["known_issues"], row["release_date"],
                    row["deprecation_date"], row["gateways"],
                    row["last_synced"], row["last_researched"],
                    row["last_updated"], row["metadata"],
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def get_model_registry(self, model_name: str) -> dict[str, Any] | None:
        conn = self.db.connect()
        try:
            row = conn.execute(
                "SELECT * FROM model_registry WHERE model_name = ?",
                (model_name,),
            ).fetchone()
            if row is None:
                return None
            return dict(ModelRegistryEntry.from_row(dict(row)).model_dump())
        finally:
            conn.close()

    def list_model_registry(
        self,
        provider: str | None = None,
        quality_tier: str | None = None,
        max_price: float | None = None,
        require_tools: bool = False,
        require_vision: bool = False,
        geo_region: str | None = None,
        gateway: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        conn = self.db.connect()
        try:
            clauses: list[str] = []
            params: list[Any] = []
            if provider is not None:
                clauses.append("provider = ?")
                params.append(provider)
            if quality_tier is not None:
                clauses.append("quality_tier = ?")
                params.append(quality_tier)
            if max_price is not None:
                clauses.append("(pricing_input IS NULL OR pricing_input <= ?)")
                params.append(max_price)
            if require_tools:
                clauses.append("supports_tools = 1")
            if require_vision:
                clauses.append("supports_vision = 1")
            if gateway is not None:
                clauses.append("gateways LIKE ?")
                params.append(f'%"{gateway}"%')

            where = " AND ".join(clauses) if clauses else "1=1"
            params.append(limit)
            rows = conn.execute(
                f"SELECT * FROM model_registry WHERE {where} "
                f"ORDER BY quality_tier ASC, pricing_input ASC LIMIT ?",
                params,
            ).fetchall()

            results = []
            for r in rows:
                entry = ModelRegistryEntry.from_row(dict(r))
                d = entry.model_dump()
                if geo_region is not None:
                    blocked = entry.geo_blocked or []
                    available = entry.geo_available or []
                    if geo_region in blocked:
                        continue
                    if available and geo_region not in available and "*" not in available:
                        continue
                results.append(d)
            return results
        finally:
            conn.close()

    def update_model_registry_synced(self, model_name: str) -> None:
        """Update the last_synced timestamp for a model."""
        from datetime import datetime

        from callmem.compat import UTC

        conn = self.db.connect()
        try:
            conn.execute(
                "UPDATE model_registry SET last_synced = ?, "
                "last_updated = ? WHERE model_name = ?",
                (
                    datetime.now(UTC).isoformat(),
                    datetime.now(UTC).isoformat(),
                    model_name,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    # ── Rewind (A6) ───────────────────────────────────────────────────

    def insert_rewind_point(self, rp: RewindPoint) -> None:
        conn = self.db.connect()
        try:
            row = rp.to_row()
            conn.execute(
                "INSERT INTO rewind_points "
                "(id, project_id, label, created_at, event_count, "
                "entity_count, metadata) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    row["id"], row["project_id"], row["label"],
                    row["created_at"], row["event_count"],
                    row["entity_count"], row["metadata"],
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def get_rewind_point(self, rp_id: str) -> RewindPoint | None:
        conn = self.db.connect()
        try:
            row = conn.execute(
                "SELECT * FROM rewind_points WHERE id = ?", (rp_id,)
            ).fetchone()
            if row is None:
                return None
            return RewindPoint.from_row(dict(row))
        finally:
            conn.close()

    def list_rewind_points(self, project_id: str) -> list[dict[str, Any]]:
        conn = self.db.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM rewind_points "
                "WHERE project_id = ? ORDER BY created_at DESC",
                (project_id,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def archive_events_after(
        self, project_id: str, timestamp: str,
    ) -> int:
        """Soft-archive events created after the given timestamp."""
        conn = self.db.connect()
        try:
            cursor = conn.execute(
                "UPDATE events SET archived_at = datetime('now') "
                "WHERE project_id = ? AND timestamp > ? "
                "AND archived_at IS NULL",
                (project_id, timestamp),
            )
            conn.commit()
            return cursor.rowcount
        finally:
            conn.close()

    def archive_entities_after(
        self, project_id: str, timestamp: str,
    ) -> int:
        """Soft-archive entities created after the given timestamp."""
        conn = self.db.connect()
        try:
            cursor = conn.execute(
                "UPDATE entities SET archived_at = datetime('now') "
                "WHERE project_id = ? AND created_at > ? "
                "AND archived_at IS NULL",
                (project_id, timestamp),
            )
            conn.commit()
            return cursor.rowcount
        finally:
            conn.close()

    def archive_tasks_after(
        self, project_id: str, timestamp: str,
    ) -> int:
        """Soft-archive tasks created after the given timestamp by marking them cancelled."""
        conn = self.db.connect()
        try:
            cursor = conn.execute(
                "UPDATE tasks SET status = 'cancelled', "
                "updated_at = datetime('now') "
                "WHERE project_id = ? AND created_at > ? "
                "AND status IN ('pending', 'in_progress')",
                (project_id, timestamp),
            )
            conn.commit()
            return cursor.rowcount
        finally:
            conn.close()

    def get_rewind_diff(
        self, project_id: str, timestamp: str,
    ) -> dict[str, Any]:
        """Show what would change if restored to the given timestamp."""
        conn = self.db.connect()
        try:
            event_count = conn.execute(
                "SELECT COUNT(*) as c FROM events "
                "WHERE project_id = ? AND timestamp > ? "
                "AND archived_at IS NULL",
                (project_id, timestamp),
            ).fetchone()["c"]

            entity_count = conn.execute(
                "SELECT COUNT(*) as c FROM entities "
                "WHERE project_id = ? AND created_at > ? "
                "AND archived_at IS NULL",
                (project_id, timestamp),
            ).fetchone()["c"]

            task_count = conn.execute(
                "SELECT COUNT(*) as c FROM tasks "
                "WHERE project_id = ? AND created_at > ? "
                "AND status IN ('pending', 'in_progress', 'completed')",
                (project_id, timestamp),
            ).fetchone()["c"]

            return {
                "events_to_archive": event_count,
                "entities_to_archive": entity_count,
                "tasks_to_cancel": task_count,
            }
        finally:
            conn.close()
