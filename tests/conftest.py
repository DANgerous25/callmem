"""Shared test fixtures for llm-mem."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from fastapi.testclient import TestClient

from llm_mem.core.database import Database
from llm_mem.core.engine import MemoryEngine
from llm_mem.core.event_bus import EventBus
from llm_mem.core.extraction import EntityExtractor
from llm_mem.core.ollama import OllamaClient
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


@pytest.fixture
def mock_ollama() -> MagicMock:
    """Mocked OllamaClient with _generate returning None by default."""
    client = MagicMock(spec=OllamaClient)
    client._generate.return_value = None
    client.is_available.return_value = False
    return client


@pytest.fixture
def event_bus() -> EventBus:
    """Real EventBus instance for testing."""
    return EventBus()


@pytest.fixture
def extractor(memory_db: Database, mock_ollama: MagicMock) -> EntityExtractor:
    """EntityExtractor with mock Ollama and no event_bus."""
    return EntityExtractor(memory_db, mock_ollama, event_bus=None)


@pytest.fixture
def ui_client(memory_db: Database) -> TestClient:
    """TestClient for the FastAPI web UI with sensitive data disabled."""
    config = Config(sensitive_data={"enabled": False, "llm_scan": False})
    engine = MemoryEngine(memory_db, config)
    from llm_mem.ui.app import create_app

    return TestClient(create_app(engine))


@pytest.fixture
def ui_client_with_data(memory_db: Database) -> TestClient:
    """TestClient with pre-seeded sessions, events, and entities."""
    config = Config(sensitive_data={"enabled": False, "llm_scan": False})
    engine = MemoryEngine(memory_db, config)
    session = engine.start_session()
    engine.ingest_one("note", "Add cursor-based pagination to list endpoints")
    engine.ingest_one("note", "Redis caching for auth module")
    engine.end_session(session.id)

    from llm_mem.models.entities import Entity

    entity = Entity(
        project_id=engine.project_id,
        source_event_id=None,
        type="todo",
        title="Add pagination tests",
        content="Write integration tests for cursor pagination",
        status="open",
        priority="high",
    )
    conn = memory_db.connect()
    try:
        row = entity.to_row()
        conn.execute(
            "INSERT INTO entities "
            "(id, project_id, source_event_id, type, title, content, "
            "status, priority, pinned, created_at, updated_at, "
            "resolved_at, metadata, archived_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row["id"], row["project_id"], row["source_event_id"],
                row["type"], row["title"], row["content"],
                row["status"], row["priority"], row["pinned"],
                row["created_at"], row["updated_at"],
                row["resolved_at"], row["metadata"], row["archived_at"],
            ),
        )
        conn.commit()
    finally:
        conn.close()

    from llm_mem.ui.app import create_app

    return TestClient(create_app(engine))


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    """Create a project directory with initialized database."""
    llm_dir = tmp_path / ".llm-mem"
    llm_dir.mkdir()
    db = Database(llm_dir / "memory.db")
    db.initialize()
    return tmp_path


@pytest.fixture
def mcp_server(project_dir: Path) -> object:
    """MCP server instance backed by project_dir."""
    from llm_mem.mcp.server import create_server

    return create_server(project_dir)
