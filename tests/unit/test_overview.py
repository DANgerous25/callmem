"""Tests for the project overview feature."""

from __future__ import annotations

from typing import TYPE_CHECKING

from callmem.core.briefing import BriefingGenerator
from callmem.core.repository import Repository
from callmem.mcp.tools import handle_set_overview
from callmem.models.config import Config
from callmem.models.projects import Project

if TYPE_CHECKING:
    from callmem.core.database import Database
    from callmem.core.engine import MemoryEngine


def _seed_project(memory_db: Database) -> str:
    repo = Repository(memory_db)
    project = Project(name="overview-test")
    repo.create_project(project)
    return project.id


class TestRepositoryOverview:
    def test_set_and_get_overview(self, memory_db: Database) -> None:
        project_id = _seed_project(memory_db)
        repo = Repository(memory_db)

        row = repo.set_overview(project_id, "My project overview text")
        assert row["content"] == "My project overview text"
        assert row["project_id"] == project_id

        fetched = repo.get_overview(project_id)
        assert fetched is not None
        assert fetched["content"] == "My project overview text"

    def test_get_overview_returns_none_when_unset(
        self, memory_db: Database,
    ) -> None:
        project_id = _seed_project(memory_db)
        repo = Repository(memory_db)
        assert repo.get_overview(project_id) is None

    def test_set_overview_upserts(self, memory_db: Database) -> None:
        project_id = _seed_project(memory_db)
        repo = Repository(memory_db)

        repo.set_overview(project_id, "First version")
        repo.set_overview(project_id, "Second version")

        row = repo.get_overview(project_id)
        assert row is not None
        assert row["content"] == "Second version"


class TestBriefingOverview:
    def test_briefing_includes_overview_when_set(
        self, memory_db: Database,
    ) -> None:
        project_id = _seed_project(memory_db)
        repo = Repository(memory_db)
        repo.set_overview(project_id, "This is a test project overview.")

        gen = BriefingGenerator(repo, Config())
        briefing = gen.generate(project_id, project_name="test")
        assert "Project Overview" in briefing.content
        assert "This is a test project overview." in briefing.content

    def test_briefing_omits_overview_when_unset(
        self, memory_db: Database,
    ) -> None:
        project_id = _seed_project(memory_db)
        repo = Repository(memory_db)

        gen = BriefingGenerator(repo, Config())
        briefing = gen.generate(project_id, project_name="test")
        assert "Project Overview" not in briefing.content

    def test_overview_truncated_to_500_tokens(
        self, memory_db: Database,
    ) -> None:
        project_id = _seed_project(memory_db)
        repo = Repository(memory_db)

        long_text = "Word " * 3000
        repo.set_overview(project_id, long_text)

        gen = BriefingGenerator(repo, Config())
        briefing = gen.generate(project_id, project_name="test")
        assert "Project Overview" in briefing.content
        assert "..." in briefing.content
        overview_section = briefing.content.split("Project Overview")[1]
        overview_section = overview_section.split("───")[0]
        from callmem.core.retrieval import _estimate_tokens
        assert _estimate_tokens(overview_section) < 600


class TestMCPSetOverview:
    def test_set_overview_via_mcp(self, engine: MemoryEngine) -> None:
        engine.start_session()
        result = handle_set_overview(
            engine, {"content": "Overview set via MCP tool."},
        )
        import json
        data = json.loads(result[0].text)
        assert data["content_length"] == len("Overview set via MCP tool.")
        assert "updated_at" in data

        overview = engine.get_overview()
        assert overview is not None
        assert overview["content"] == "Overview set via MCP tool."

    def test_set_overview_empty_content_errors(
        self, engine: MemoryEngine,
    ) -> None:
        engine.start_session()
        result = handle_set_overview(engine, {"content": "  "})
        import json
        data = json.loads(result[0].text)
        assert "error" in data


class TestReextractPreservesOverview:
    def test_re_extract_does_not_touch_overview(
        self, memory_db: Database,
    ) -> None:
        project_id = _seed_project(memory_db)
        repo = Repository(memory_db)
        repo.set_overview(project_id, "Overview before re-extract.")

        from callmem.core.reextraction import ReExtractor
        from callmem.core.ollama import OllamaClient
        from callmem.models.config import Config

        config = Config()
        config.llm.backend = "none"
        reextractor = ReExtractor(memory_db, OllamaClient(), config)
        reextractor.run(project_id=project_id)

        row = repo.get_overview(project_id)
        assert row is not None
        assert row["content"] == "Overview before re-extract."
