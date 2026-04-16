"""Shared test fixtures for llm-mem."""

from __future__ import annotations

from pathlib import Path

import pytest

from llm_mem.core.database import Database


@pytest.fixture
def tmp_db(tmp_path: Path) -> Database:
    """Create a temporary database with schema initialized."""
    db = Database(tmp_path / "test.db")
    db.initialize()
    return db


@pytest.fixture
def memory_db() -> Database:
    """Create an in-memory database with schema initialized."""
    db = Database(":memory:")
    db.initialize()
    return db
