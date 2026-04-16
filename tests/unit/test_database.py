"""Tests for database initialization and migrations."""

from __future__ import annotations

from pathlib import Path

import pytest

from llm_mem.core.database import Database


def test_database_creates_all_tables(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db")
    db.initialize()
    tables = db.list_tables()
    expected = [
        "projects", "sessions", "events", "entities", "summaries",
        "memory_edges", "compaction_log", "config", "schema_version", "jobs",
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
    assert version == 1


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
    assert db.get_schema_version() == 1


def test_foreign_keys_enabled(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db")
    db.initialize()
    conn = db.connect()
    try:
        fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        assert fk == 1
    finally:
        conn.close()


def test_in_memory_database() -> None:
    db = Database(":memory:")
    db.initialize()
    tables = db.list_tables()
    assert "events" in tables
    assert db.get_schema_version() == 1
