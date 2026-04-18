"""Tests for re-extraction command and engine."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from llm_mem.core.engine import MemoryEngine
from llm_mem.core.ollama import OllamaClient
from llm_mem.core.reextraction import ReExtractor
from llm_mem.models.config import Config

if TYPE_CHECKING:
    from llm_mem.core.database import Database


def _setup_with_events(memory_db: Database) -> tuple[MemoryEngine, ReExtractor]:
    config = Config(sensitive_data={"enabled": False, "llm_scan": False})
    engine = MemoryEngine(memory_db, config)
    ollama = OllamaClient()
    re_extractor = ReExtractor(memory_db, ollama, config)
    return engine, re_extractor


class TestReExtractorCounts:
    def test_counts_events(self, memory_db: Database) -> None:
        engine, re_extractor = _setup_with_events(memory_db)
        engine.start_session()
        engine.ingest_one("note", "event 1")
        engine.ingest_one("note", "event 2")

        project_id = engine.project_id
        count = re_extractor.count_events(project_id)
        assert count == 2

    def test_counts_events_with_session_filter(self, memory_db: Database) -> None:
        engine, re_extractor = _setup_with_events(memory_db)
        engine.start_session()
        engine.ingest_one("note", "event 1")

        s2 = engine.start_session()
        engine.ingest_one("note", "event 2")
        engine.ingest_one("note", "event 3")

        project_id = engine.project_id
        count = re_extractor.count_events(project_id, session_id=s2.id)
        assert count == 2

    def test_counts_sessions(self, memory_db: Database) -> None:
        engine, re_extractor = _setup_with_events(memory_db)
        engine.start_session()
        engine.start_session()

        project_id = engine.project_id
        count = re_extractor.count_sessions(project_id)
        assert count >= 2


class TestReExtractorDryRun:
    def test_dry_run_does_not_modify_db(self, memory_db: Database) -> None:
        engine, re_extractor = _setup_with_events(memory_db)
        engine.start_session()
        engine.ingest_one("note", "some event")

        project_id = engine.project_id
        result = re_extractor.run(project_id, dry_run=True)

        assert result["dry_run"] is True
        assert result["total_events"] == 1
        assert result["batches"] >= 1

        conn = memory_db.connect()
        try:
            row = conn.execute(
                "SELECT COUNT(*) as c FROM entities"
            ).fetchone()
            assert row["c"] == 0
        finally:
            conn.close()


class TestReExtractorArchivesEntities:
    def test_archives_old_entities(self, memory_db: Database) -> None:
        engine, re_extractor = _setup_with_events(memory_db)
        ollama = OllamaClient()
        engine.start_session()
        event = engine.ingest_one("note", "Use Redis for caching")
        assert event is not None

        llm_response = (
            '{"decisions": [{"title": "Use Redis", "content": "Caching"}],'
            '"todos": [], "facts": [], "failures": [], "discoveries": [], '
            '"features": [], "bugfixes": [], "research": [], "changes": []}'
        )

        from llm_mem.core.extraction import EntityExtractor

        extractor = EntityExtractor(memory_db, ollama)
        with patch.object(extractor.ollama, "_generate", return_value=llm_response):
            extractor.process_pending()

        conn = memory_db.connect()
        try:
            before = conn.execute(
                "SELECT COUNT(*) as c FROM entities WHERE archived_at IS NULL"
            ).fetchone()
            assert before["c"] == 1
        finally:
            conn.close()

        new_llm_response = (
            '{"decisions": [{"title": "Use Redis v2", "content": "Better caching"}],'
            '"todos": [], "facts": [], "failures": [], "discoveries": [], '
            '"features": [], "bugfixes": [], "research": [], "changes": []}'
        )

        project_id = engine.project_id
        with patch.object(re_extractor.ollama, "_generate", return_value=new_llm_response):
            result = re_extractor.run(project_id, force=True)

        assert result["entities_created"] >= 1
        assert result["events_processed"] >= 1

        conn = memory_db.connect()
        try:
            archived = conn.execute(
                "SELECT COUNT(*) as c FROM entities WHERE archived_at IS NOT NULL"
            ).fetchone()
            active = conn.execute(
                "SELECT COUNT(*) as c FROM entities WHERE archived_at IS NULL"
            ).fetchone()
            assert archived["c"] == 1
            assert active["c"] == 1
        finally:
            conn.close()


class TestReExtractorPreservesEdits:
    def test_preserves_pinned_entities(self, memory_db: Database) -> None:
        engine, re_extractor = _setup_with_events(memory_db)
        engine.start_session()
        event = engine.ingest_one("note", "Use Redis for caching")
        assert event is not None

        from llm_mem.core.extraction import EntityExtractor

        extractor = EntityExtractor(memory_db, OllamaClient())
        llm_response = (
            '{"decisions": [{"title": "Use Redis", "content": "Caching"}],'
            '"todos": [], "facts": [], "failures": [], "discoveries": [], '
            '"features": [], "bugfixes": [], "research": [], "changes": []}'
        )
        with patch.object(extractor.ollama, "_generate", return_value=llm_response):
            entities = extractor.process_pending()
        assert len(entities) == 1

        engine.repo.set_pinned(entities[0].id, True)

        new_llm_response = (
            '{"decisions": [{"title": "Use Redis v2", "content": "Better"}],'
            '"todos": [], "facts": [], "failures": [], "discoveries": [], '
            '"features": [], "bugfixes": [], "research": [], "changes": []}'
        )

        project_id = engine.project_id
        with patch.object(re_extractor.ollama, "_generate", return_value=new_llm_response):
            re_extractor.run(project_id, force=False)

        conn = memory_db.connect()
        try:
            pinned = conn.execute(
                "SELECT * FROM entities WHERE pinned = 1 AND archived_at IS NULL"
            ).fetchone()
            assert pinned is not None
            assert pinned["title"] == "Use Redis"
        finally:
            conn.close()

    def test_force_overwrites_pinned(self, memory_db: Database) -> None:
        engine, re_extractor = _setup_with_events(memory_db)
        engine.start_session()
        event = engine.ingest_one("note", "Use Redis for caching")
        assert event is not None

        from llm_mem.core.extraction import EntityExtractor

        extractor = EntityExtractor(memory_db, OllamaClient())
        llm_response = (
            '{"decisions": [{"title": "Use Redis", "content": "Caching"}],'
            '"todos": [], "facts": [], "failures": [], "discoveries": [], '
            '"features": [], "bugfixes": [], "research": [], "changes": []}'
        )
        with patch.object(extractor.ollama, "_generate", return_value=llm_response):
            entities = extractor.process_pending()
        assert len(entities) == 1

        engine.repo.set_pinned(entities[0].id, True)

        new_llm_response = (
            '{"decisions": [{"title": "Use Redis v2", "content": "Better"}],'
            '"todos": [], "facts": [], "failures": [], "discoveries": [], '
            '"features": [], "bugfixes": [], "research": [], "changes": []}'
        )

        project_id = engine.project_id
        with patch.object(re_extractor.ollama, "_generate", return_value=new_llm_response):
            re_extractor.run(project_id, force=True)

        conn = memory_db.connect()
        try:
            archived = conn.execute(
                "SELECT COUNT(*) as c FROM entities WHERE archived_at IS NOT NULL"
            ).fetchone()
            active = conn.execute(
                "SELECT COUNT(*) as c FROM entities WHERE archived_at IS NULL"
            ).fetchone()
            assert archived["c"] == 1
            assert active["c"] == 1
        finally:
            conn.close()


class TestReExtractorSessionFilter:
    def test_limits_to_single_session(self, memory_db: Database) -> None:
        engine, re_extractor = _setup_with_events(memory_db)

        s1 = engine.start_session()
        engine.ingest_one("note", "event in session 1")

        engine.start_session()
        engine.ingest_one("note", "event in session 2")

        project_id = engine.project_id
        result = re_extractor.run(project_id, session_id=s1.id, dry_run=True)
        assert result["total_events"] == 1


class TestReExtractCLI:
    def test_dry_run(self, memory_db: Database) -> None:
        from llm_mem.cli import main

        config = Config(sensitive_data={"enabled": False, "llm_scan": False})
        engine = MemoryEngine(memory_db, config)
        engine.start_session()
        engine.ingest_one("note", "test event")

        runner = CliRunner()
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            from llm_mem.core.database import Database

            project_dir = Path(tmpdir)
            llm_mem_dir = project_dir / ".llm-mem"
            llm_mem_dir.mkdir()
            db = Database(llm_mem_dir / "memory.db")
            db.initialize()

            cfg = Config(
                sensitive_data={"enabled": False, "llm_scan": False},
            )
            eng = MemoryEngine(db, cfg)
            eng.start_session()
            eng.ingest_one("note", "test event")

            config_path = llm_mem_dir / "config.toml"
            config_path.write_text(
                '[project]\nname = "test"\n[llm]\nbackend = "ollama"\n[ollama]\nmodel = "test"\n'
            )

            with patch("llm_mem.core.engine._create_llm_client") as mock_create:
                mock_llm = MagicMock()
                mock_llm.is_available.return_value = True
                mock_llm._generate.return_value = '{"decisions":[],"todos":[]}'
                mock_create.return_value = mock_llm

                result = runner.invoke(
                    main, ["re-extract", "--dry-run", "--project", str(project_dir)]
                )

        assert result.exit_code == 0
        assert "events" in result.output.lower() or "Sessions" in result.output

    def test_no_db_shows_error(self) -> None:
        from llm_mem.cli import main

        runner = CliRunner()
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            result = runner.invoke(
                main, ["re-extract", "--project", tmpdir]
            )
        assert "No llm-mem database" in result.output


class TestParseSince:
    def test_parses_days(self, memory_db: Database) -> None:
        _, re_extractor = _setup_with_events(memory_db)
        result = re_extractor._parse_since("7d")
        assert result is not None

    def test_parses_hours(self, memory_db: Database) -> None:
        _, re_extractor = _setup_with_events(memory_db)
        result = re_extractor._parse_since("24h")
        assert result is not None

    def test_passthrough_iso(self, memory_db: Database) -> None:
        _, re_extractor = _setup_with_events(memory_db)
        result = re_extractor._parse_since("2025-01-01T00:00:00")
        assert result == "2025-01-01T00:00:00"

    def test_empty_returns_none(self, memory_db: Database) -> None:
        _, re_extractor = _setup_with_events(memory_db)
        result = re_extractor._parse_since("")
        assert result is None
