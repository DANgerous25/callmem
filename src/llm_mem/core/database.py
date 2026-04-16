"""SQLite database manager with migration support."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any


class Database:
    """Manages SQLite connections, schema initialization, and migrations.

    Usage:
        db = Database(Path("project/.llm-mem/memory.db"))
        db.initialize()

        with db.connection() as conn:
            conn.execute("SELECT ...")
    """

    MIGRATIONS_DIR = Path(__file__).parent / "migrations"

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path) if not isinstance(db_path, Path) else db_path
        self._is_memory = str(self.db_path) == ":memory:"
        if self._is_memory:
            # Use a named in-memory DB via URI so multiple connect() calls
            # share the same database. Keep one connection alive to prevent
            # the shared cache from being garbage-collected.
            import uuid

            self._uri = f"file:llm_mem_{uuid.uuid4().hex}?mode=memory&cache=shared"
            self._keepalive = sqlite3.connect(self._uri, uri=True)
        self._ensure_parent_dir()

    def _ensure_parent_dir(self) -> None:
        if not self._is_memory:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        """Create a new connection with recommended pragmas."""
        if self._is_memory:
            conn = sqlite3.connect(self._uri, uri=True)
        else:
            conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        if not self._is_memory:
            conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def initialize(self) -> None:
        """Run all pending migrations to bring the schema up to date."""
        conn = self.connect()
        try:
            self._ensure_schema_version_table(conn)
            current_version = self._get_schema_version(conn)
            migrations = self._load_migrations()

            for version, sql in migrations:
                if version > current_version:
                    conn.executescript(sql)
                    conn.execute(
                        "INSERT INTO schema_version (version, applied_at, description) "
                        "VALUES (?, datetime('now'), ?)",
                        (version, f"Migration {version:03d}"),
                    )
                    conn.commit()
        finally:
            conn.close()

    def get_schema_version(self) -> int:
        """Return the current schema version."""
        conn = self.connect()
        try:
            return self._get_schema_version(conn)
        finally:
            conn.close()

    def list_tables(self) -> list[str]:
        """Return names of all tables (including virtual tables)."""
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'virtual table') "
                "ORDER BY name"
            ).fetchall()
            return [row["name"] for row in rows]
        finally:
            conn.close()

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        """Execute a single SQL statement and return the cursor. For quick one-off queries."""
        conn = self.connect()
        try:
            cursor = conn.execute(sql, params)
            conn.commit()
            return cursor
        finally:
            conn.close()

    def _ensure_schema_version_table(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_version ("
            "  version INTEGER PRIMARY KEY,"
            "  applied_at TEXT NOT NULL,"
            "  description TEXT"
            ")"
        )
        conn.commit()

    def _get_schema_version(self, conn: sqlite3.Connection) -> int:
        row = conn.execute("SELECT MAX(version) as v FROM schema_version").fetchone()
        return row["v"] or 0

    def _load_migrations(self) -> list[tuple[int, str]]:
        """Load migration SQL files sorted by version number."""
        if not self.MIGRATIONS_DIR.exists():
            return []

        migrations: list[tuple[int, str]] = []
        for path in sorted(self.MIGRATIONS_DIR.glob("*.sql")):
            version = int(path.stem.split("_")[0])
            sql = path.read_text()
            migrations.append((version, sql))
        return migrations
