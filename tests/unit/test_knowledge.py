"""Tests for the KnowledgeAgent."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

from llm_mem.core.knowledge import KnowledgeAgent
from llm_mem.core.ollama import OllamaClient
from llm_mem.core.repository import Repository
from llm_mem.models.entities import Entity
from llm_mem.models.projects import Project
from llm_mem.models.sessions import Session

if TYPE_CHECKING:
    from llm_mem.core.database import Database


def _seed_project(memory_db: Database) -> str:
    repo = Repository(memory_db)
    project = Project(name="test-project")
    repo.create_project(project)
    return project.id


def _seed_entities(memory_db: Database) -> str:
    project_id = _seed_project(memory_db)

    session = Session(project_id=project_id)
    repo = Repository(memory_db)
    repo.insert_session(session)

    entities = [
        Entity(
            project_id=project_id,
            type="decision",
            title="Use SQLite",
            content="Chose SQLite for storage",
        ),
        Entity(
            project_id=project_id,
            type="feature",
            title="Added auth",
            content="Implemented JWT auth",
        ),
        Entity(
            project_id=project_id,
            type="bugfix",
            title="Fixed null pointer",
            content="Fixed NPE in handler",
        ),
    ]
    for e in entities:
        _insert_entity(memory_db, e)

    return project_id


def _insert_entity(memory_db: Database, entity: Entity) -> None:
    conn = memory_db.connect()
    try:
        row = entity.to_row()
        conn.execute(
            "INSERT INTO entities "
            "(id, project_id, source_event_id, type, title, content, "
            "key_points, synopsis, "
            "status, priority, pinned, created_at, updated_at, "
            "resolved_at, metadata, archived_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row["id"], row["project_id"], row["source_event_id"],
                row["type"], row["title"], row["content"],
                row["key_points"], row["synopsis"],
                row["status"], row["priority"], row["pinned"],
                row["created_at"], row["updated_at"],
                row["resolved_at"], row["metadata"], row["archived_at"],
            ),
        )
        conn.commit()
    finally:
        conn.close()


class TestKnowledgeAgent:
    def test_build_corpus(self, memory_db: Database) -> None:
        project_id = _seed_entities(memory_db)
        agent = KnowledgeAgent(memory_db, OllamaClient())
        result = agent.build_corpus("test-corpus", project_id=project_id)
        assert result["entity_count"] == 3
        assert result["name"] == "test-corpus"
        assert result["token_count"] > 0

    def test_list_corpora(self, memory_db: Database) -> None:
        project_id = _seed_entities(memory_db)
        agent = KnowledgeAgent(memory_db, OllamaClient())
        agent.build_corpus("corpus-a", project_id=project_id)
        agent.build_corpus("corpus-b", project_id=project_id)
        corpora = agent.list_corpora()
        assert len(corpora) == 2
        names = {c["name"] for c in corpora}
        assert names == {"corpus-a", "corpus-b"}

    def test_query_corpus(self, memory_db: Database) -> None:
        project_id = _seed_entities(memory_db)
        agent = KnowledgeAgent(memory_db, OllamaClient())
        agent.build_corpus("test", project_id=project_id)

        with patch.object(
            agent.ollama, "_generate",
            return_value="SQLite was chosen for storage",
        ):
            answer = agent.query_corpus("test", "Why SQLite?")
        assert "SQLite" in answer

    def test_rebuild_corpus(self, memory_db: Database) -> None:
        project_id = _seed_entities(memory_db)
        agent = KnowledgeAgent(memory_db, OllamaClient())
        agent.build_corpus("test", project_id=project_id)

        result = agent.rebuild_corpus("test")
        assert result["entity_count"] == 3
        assert result["name"] == "test"

    def test_delete_corpus(self, memory_db: Database) -> None:
        project_id = _seed_entities(memory_db)
        agent = KnowledgeAgent(memory_db, OllamaClient())
        agent.build_corpus("test", project_id=project_id)
        agent.delete_corpus("test")
        assert agent.list_corpora() == []

    def test_build_corpus_with_type_filter(
        self, memory_db: Database
    ) -> None:
        project_id = _seed_entities(memory_db)
        agent = KnowledgeAgent(memory_db, OllamaClient())
        result = agent.build_corpus(
            "decisions-only",
            project_id=project_id,
            types=["decision"],
        )
        assert result["entity_count"] == 1

    def test_query_nonexistent_corpus(
        self, memory_db: Database
    ) -> None:
        agent = KnowledgeAgent(memory_db, OllamaClient())
        answer = agent.query_corpus("nope", "anything")
        assert "empty" in answer.lower() or "not found" in answer.lower()
