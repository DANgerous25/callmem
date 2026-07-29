"""Tests for database initialization and migrations."""

from __future__ import annotations

from typing import TYPE_CHECKING

from callmem.core.database import Database

if TYPE_CHECKING:
    from pathlib import Path


def test_database_creates_all_tables(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db")
    db.initialize()
    tables = db.list_tables()
    expected = [
        "projects", "sessions", "events", "entities", "summaries",
        "memory_edges", "compaction_log", "config", "schema_version", "jobs",
        "vault",
    ]
    for table in expected:
        assert table in tables, f"Missing table: {table}"


def test_fts5_tables_created(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db")
    db.initialize()
    tables = db.list_tables()
    assert "events_fts" in tables
    assert "entities_fts" in tables
    assert "summaries_fts" in tables


def test_schema_version_set(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db")
    db.initialize()
    version = db.get_schema_version()
    assert version == 22


def test_migration_numbers_have_no_gaps() -> None:
    """A gap would pin upgraded DBs past the missing number forever.

    get_schema_version() is MAX(version) and migrate() applies only
    version > current, so a migration file merged later with a lower
    number than one already applied would silently never run.
    """
    numbers = sorted(
        int(p.name.split("_", 1)[0])
        for p in Database.MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql")
    )
    assert numbers == list(range(1, len(numbers) + 1))


def test_wal_mode_enabled(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db")
    db.initialize()
    conn = db.connect()
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal"
    finally:
        conn.close()


def test_idempotent_init(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db")
    db.initialize()
    db.initialize()  # Should not raise
    assert db.get_schema_version() == 22


def test_foreign_keys_enabled(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db")
    db.initialize()
    conn = db.connect()
    try:
        fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        assert fk == 1
    finally:
        conn.close()


def test_triggers_created(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db")
    db.initialize()
    conn = db.connect()
    try:
        triggers = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' ORDER BY name"
        ).fetchall()
        trigger_names = [t["name"] for t in triggers]
        expected = [
            "entities_ad", "entities_ai", "entities_au",
            "events_ad", "events_ai", "events_au",
            "summaries_ad", "summaries_ai", "summaries_au",
        ]
        for t in expected:
            assert t in trigger_names, f"Missing trigger: {t}"
    finally:
        conn.close()


def test_migration_016_on_populated_v15_database(tmp_path: Path) -> None:
    """A job row inserted before the backoff/requeue-count migration must
    dequeue and behave exactly as before: NULL next_attempt_at is treated
    as immediately ready, and requeue_count defaults to 0."""
    import sqlite3

    from callmem.core.queue import JobQueue

    db_path = tmp_path / "test.db"
    db = Database(db_path)
    db.initialize()

    # Simulate a v15 row inserted before this migration existed (no
    # next_attempt_at / requeue_count columns to write to at the time).
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO jobs (id, type, payload, status, attempts, max_attempts, "
        "created_at) VALUES ('legacy-job', 'extract_entities', '{}', "
        "'pending', 0, 3, datetime('now'))"
    )
    conn.commit()
    conn.close()

    queue = JobQueue(db)
    job = queue.get_job("legacy-job")
    assert job is not None
    assert job.next_attempt_at is None
    assert job.requeue_count == 0

    dequeued = queue.dequeue("extract_entities")
    assert dequeued is not None
    assert dequeued.id == "legacy-job"


def test_migration_017_on_populated_database(tmp_path: Path) -> None:
    """An entity row inserted before the source_event_ids migration must
    survive with source_event_ids NULL, and the provenance fallback to
    the single source_event_id column must still work."""
    import sqlite3

    from callmem.core.repository import Repository

    db_path = tmp_path / "test.db"
    db = Database(db_path)
    db.initialize()

    # Simulate a pre-017 row inserted before this migration existed (no
    # source_event_ids column to write to at the time — only the single
    # source_event_id column).
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO projects (id, name, created_at, updated_at) "
        "VALUES ('proj-1', 'legacy-project', datetime('now'), datetime('now'))"
    )
    conn.execute(
        "INSERT INTO entities (id, project_id, source_event_id, type, title, "
        "content, created_at, updated_at) VALUES ('legacy-entity', 'proj-1', "
        "'event-1', 'todo', 'Legacy todo', 'Pre-migration content', "
        "datetime('now'), datetime('now'))"
    )
    conn.commit()

    row = conn.execute(
        "SELECT source_event_id, source_event_ids FROM entities WHERE id = ?",
        ("legacy-entity",),
    ).fetchone()
    conn.close()
    assert row[0] == "event-1"
    assert row[1] is None

    repo = Repository(db)
    archived = repo.archive_entities_with_full_coverage(
        project_id="proj-1",
        covered_event_ids={"event-1"},
        candidate_ids={"legacy-entity"},
    )
    assert archived == 1


def test_migration_019_on_populated_database(tmp_path: Path) -> None:
    """An entity row inserted before the citation-persistence migration
    must survive with cited_count = 0 and last_cited_at = NULL — the same
    as "never cited", so scoring behaves no worse than before the column
    existed."""
    import sqlite3

    db_path = tmp_path / "test.db"
    db = Database(db_path)
    db.initialize()

    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO projects (id, name, created_at, updated_at) "
        "VALUES ('proj-1', 'legacy-project', datetime('now'), datetime('now'))"
    )
    conn.execute(
        "INSERT INTO entities (id, project_id, source_event_id, type, title, "
        "content, created_at, updated_at) VALUES ('legacy-entity', 'proj-1', "
        "'event-1', 'todo', 'Legacy todo', 'Pre-migration content', "
        "datetime('now'), datetime('now'))"
    )
    conn.commit()

    row = conn.execute(
        "SELECT cited_count, last_cited_at FROM entities WHERE id = ?",
        ("legacy-entity",),
    ).fetchone()
    conn.close()
    assert row[0] == 0
    assert row[1] is None


def test_in_memory_database() -> None:
    db = Database(":memory:")
    db.initialize()
    tables = db.list_tables()
    assert "events" in tables
    assert db.get_schema_version() == 22
