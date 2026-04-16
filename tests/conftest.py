"""Shared test fixtures for llm-mem."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from llm_mem.core.database import Database
from llm_mem.core.engine import MemoryEngine
from llm_mem.core.repository import Repository
from llm_mem.models.config import Config


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


@pytest.fixture
def repo(memory_db: Database) -> Repository:
    """Create a repository backed by an in-memory database."""
    return Repository(memory_db)


@pytest.fixture
def engine(memory_db: Database) -> MemoryEngine:
    """Create an engine with a fresh in-memory database and default config."""
    config = Config()
    return MemoryEngine(memory_db, config)


@pytest.fixture
def engine_with_auto_start(memory_db: Database) -> MemoryEngine:
    """Engine that auto-creates sessions on ingest."""
    config = Config()
    return MemoryEngine(memory_db, config)
