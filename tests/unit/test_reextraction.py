"""Tests for re-extraction command and engine."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from callmem.core.engine import MemoryEngine
from callmem.core.ollama import OllamaClient
from callmem.core.reextraction import ReExtractor
from callmem.models.config import Config

if TYPE_CHECKING:
    from callmem.core.database import Database


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

        from callmem.core.extraction import EntityExtractor

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

        from callmem.core.extraction import EntityExtractor

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

        from callmem.core.extraction import EntityExtractor

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


class TestReExtractorFailureHandling:
    def test_extract_batch_raises_on_none_response(
        self, memory_db: Database
    ) -> None:
        """A None transport response is a failure, not 'no entities'
        (phase0-reliability task 6)."""
        engine, re_extractor = _setup_with_events(memory_db)
        engine.start_session()
        event = engine.ingest_one("note", "some content")
        assert event is not None

        events = [{
            "id": event.id,
            "project_id": engine.project_id,
            "type": "note",
            "content": "some content",
        }]

        with patch.object(re_extractor.ollama, "_generate", return_value=None):
            with pytest.raises(Exception):
                re_extractor._extract_batch(events)

    def test_extract_batch_raises_on_empty_response(
        self, memory_db: Database
    ) -> None:
        engine, re_extractor = _setup_with_events(memory_db)
        engine.start_session()
        event = engine.ingest_one("note", "some content")
        assert event is not None

        events = [{
            "id": event.id,
            "project_id": engine.project_id,
            "type": "note",
            "content": "some content",
        }]

        with patch.object(re_extractor.ollama, "_generate", return_value=""):
            with pytest.raises(Exception):
                re_extractor._extract_batch(events)

    def test_failed_batch_leaves_prior_entities_unarchived_and_is_counted(
        self, memory_db: Database
    ) -> None:
        """Re-extraction must extract FIRST and only archive prior entities
        for a batch once extraction succeeds. A failing batch must leave
        its prior entities untouched, be counted, and the run must
        continue to the next batch (phase0-reliability task 6)."""
        engine, re_extractor = _setup_with_events(memory_db)
        engine.start_session()
        e1 = engine.ingest_one("note", "first event content")
        e2 = engine.ingest_one("note", "second event content")
        assert e1 is not None and e2 is not None

        from callmem.core.extraction import EntityExtractor

        extractor = EntityExtractor(memory_db, OllamaClient())
        seed_response = (
            '{"decisions": [{"title": "Original decision", "content": "c"}],'
            '"todos": [], "facts": [], "failures": [], "discoveries": [], '
            '"features": [], "bugfixes": [], "research": [], "changes": []}'
        )
        with patch.object(extractor.ollama, "_generate", return_value=seed_response):
            seeded = extractor.process_pending()
        assert len(seeded) == 2

        project_id = engine.project_id
        success_response = (
            '{"decisions": [{"title": "Replacement decision", "content": "c2"}],'
            '"todos": [], "facts": [], "failures": [], "discoveries": [], '
            '"features": [], "bugfixes": [], "research": [], "changes": []}'
        )

        with patch.object(
            re_extractor.ollama, "_generate",
            side_effect=[success_response, None],
        ):
            result = re_extractor.run(project_id, batch_size=1, force=True)

        assert result["failed_batches"] == 1

        conn = memory_db.connect()
        try:
            archived = conn.execute(
                "SELECT COUNT(*) as c FROM entities WHERE archived_at IS NOT NULL"
            ).fetchone()
            active = conn.execute(
                "SELECT * FROM entities WHERE archived_at IS NULL "
                "ORDER BY created_at ASC"
            ).fetchall()
        finally:
            conn.close()

        # Only the successful batch's prior entity was archived.
        assert archived["c"] == 1
        # The failed batch's prior entity is still active/unarchived.
        titles = {r["title"] for r in active}
        assert "Original decision" in titles
        assert "Replacement decision" in titles


class TestReExtractorPartialCoverage:
    """An entity's source_event_ids may span several re-extraction
    batches when --batch-size differs from the batch size used for the
    original extraction. Archiving must wait until ALL of an entity's
    source events are confirmed re-extracted — archiving as soon as any
    one covering batch succeeds would silently lose the events any
    later, failing sibling batch never got to re-extract (Fix round 1,
    phase0-reliability task 6)."""

    def test_incomplete_coverage_after_batch_failure_does_not_archive(
        self, memory_db: Database
    ) -> None:
        from callmem.models.events import EventInput

        engine, re_extractor = _setup_with_events(memory_db)
        engine.start_session()
        events = engine.ingest([
            EventInput(type="note", content=f"event {i}") for i in range(1, 5)
        ])
        assert len(events) == 4
        event_ids = [e.id for e in events]

        from callmem.core.extraction import EntityExtractor

        extractor = EntityExtractor(memory_db, OllamaClient())
        seed_response = (
            '{"decisions": [{"title": "Original decision", "content": "c"}],'
            '"todos": [], "facts": [], "failures": [], "discoveries": [], '
            '"features": [], "bugfixes": [], "research": [], "changes": []}'
        )
        with patch.object(extractor.ollama, "_generate", return_value=seed_response):
            seeded = extractor.process_pending()
        assert len(seeded) == 1
        original = seeded[0]
        assert original.source_event_ids == event_ids

        project_id = engine.project_id
        response_a = (
            '{"decisions": [{"title": "Replacement A", "content": "c2"}],'
            '"todos": [], "facts": [], "failures": [], "discoveries": [], '
            '"features": [], "bugfixes": [], "research": [], "changes": []}'
        )

        # batch_size=2 splits the 4 events differently than the original
        # single 4-event batch: [e1,e2] succeeds, [e3,e4] fails.
        with patch.object(
            re_extractor.ollama, "_generate",
            side_effect=[response_a, None],
        ):
            result = re_extractor.run(project_id, batch_size=2, force=True)

        assert result["failed_batches"] == 1
        assert result["entities_archived"] == 0

        conn = memory_db.connect()
        try:
            row = conn.execute(
                "SELECT archived_at FROM entities WHERE id = ?",
                (original.id,),
            ).fetchone()
            active_titles = {
                r["title"] for r in conn.execute(
                    "SELECT title FROM entities WHERE archived_at IS NULL"
                ).fetchall()
            }
        finally:
            conn.close()

        assert row["archived_at"] is None, (
            "original entity archived before all its source events "
            "were successfully re-extracted — this is the data-loss bug"
        )
        # Temporary duplicate accepted: original stays visible alongside
        # the partial replacement until a fully-successful run converges.
        assert "Original decision" in active_titles
        assert "Replacement A" in active_titles

    def test_full_coverage_across_differing_batch_size_archives_once(
        self, memory_db: Database
    ) -> None:
        from callmem.models.events import EventInput

        engine, re_extractor = _setup_with_events(memory_db)
        engine.start_session()
        events = engine.ingest([
            EventInput(type="note", content=f"event {i}") for i in range(1, 5)
        ])
        event_ids = [e.id for e in events]

        from callmem.core.extraction import EntityExtractor

        extractor = EntityExtractor(memory_db, OllamaClient())
        seed_response = (
            '{"decisions": [{"title": "Original decision", "content": "c"}],'
            '"todos": [], "facts": [], "failures": [], "discoveries": [], '
            '"features": [], "bugfixes": [], "research": [], "changes": []}'
        )
        with patch.object(extractor.ollama, "_generate", return_value=seed_response):
            seeded = extractor.process_pending()
        assert len(seeded) == 1
        original = seeded[0]
        assert original.source_event_ids == event_ids

        project_id = engine.project_id
        response_a = (
            '{"decisions": [{"title": "Replacement A", "content": "c2"}],'
            '"todos": [], "facts": [], "failures": [], "discoveries": [], '
            '"features": [], "bugfixes": [], "research": [], "changes": []}'
        )
        response_b = (
            '{"decisions": [{"title": "Replacement B", "content": "c3"}],'
            '"todos": [], "facts": [], "failures": [], "discoveries": [], '
            '"features": [], "bugfixes": [], "research": [], "changes": []}'
        )

        # batch_size=2 (differs from the original 4-event batch): both
        # sub-batches succeed, so the original must be archived exactly
        # once, after the second (covering) batch completes.
        with patch.object(
            re_extractor.ollama, "_generate",
            side_effect=[response_a, response_b],
        ):
            result = re_extractor.run(project_id, batch_size=2, force=True)

        assert result["failed_batches"] == 0
        assert result["entities_archived"] == 1

        conn = memory_db.connect()
        try:
            row = conn.execute(
                "SELECT archived_at FROM entities WHERE id = ?",
                (original.id,),
            ).fetchone()
            active_titles = {
                r["title"] for r in conn.execute(
                    "SELECT title FROM entities WHERE archived_at IS NULL"
                ).fetchall()
            }
        finally:
            conn.close()

        assert row["archived_at"] is not None
        assert active_titles == {"Replacement A", "Replacement B"}

    def test_legacy_null_source_event_ids_falls_back_to_source_event_id(
        self, memory_db: Database
    ) -> None:
        """A pre-migration row (source_event_ids column still NULL) must
        remain archivable via the source_event_id fallback once that
        single event is fully covered."""
        from callmem.models.entities import Entity

        engine, re_extractor = _setup_with_events(memory_db)
        engine.start_session()
        event = engine.ingest_one("note", "legacy event")
        assert event is not None

        legacy = Entity(
            project_id=engine.project_id,
            source_event_id=event.id,
            type="decision",
            title="Legacy decision",
            content="Predates source_event_ids",
        )
        conn = memory_db.connect()
        try:
            row = legacy.to_row()
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

        project_id = engine.project_id
        success_response = (
            '{"decisions": [{"title": "New decision", "content": "c"}],'
            '"todos": [], "facts": [], "failures": [], "discoveries": [], '
            '"features": [], "bugfixes": [], "research": [], "changes": []}'
        )
        with patch.object(
            re_extractor.ollama, "_generate", return_value=success_response,
        ):
            result = re_extractor.run(project_id, force=True)

        assert result["entities_archived"] == 1

        conn = memory_db.connect()
        try:
            row = conn.execute(
                "SELECT archived_at FROM entities WHERE id = ?",
                (legacy.id,),
            ).fetchone()
        finally:
            conn.close()
        assert row["archived_at"] is not None


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
        from callmem.cli import main

        config = Config(sensitive_data={"enabled": False, "llm_scan": False})
        engine = MemoryEngine(memory_db, config)
        engine.start_session()
        engine.ingest_one("note", "test event")

        runner = CliRunner()
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            from callmem.core.database import Database

            project_dir = Path(tmpdir)
            callmem_dir = project_dir / ".callmem"
            callmem_dir.mkdir()
            db = Database(callmem_dir / "memory.db")
            db.initialize()

            cfg = Config(
                sensitive_data={"enabled": False, "llm_scan": False},
            )
            eng = MemoryEngine(db, cfg)
            eng.start_session()
            eng.ingest_one("note", "test event")

            config_path = callmem_dir / "config.toml"
            config_path.write_text(
                '[project]\nname = "test"\n[llm]\nbackend = "ollama"\n[ollama]\nmodel = "test"\n'
            )

            with patch("callmem.core.engine._create_llm_client") as mock_create:
                mock_llm = MagicMock()
                mock_llm.is_available.return_value = True
                mock_llm._generate.return_value = '{"decisions":[],"todos":[]}'
                mock_create.return_value = mock_llm

                result = runner.invoke(
                    main, ["re-extract", "--dry-run", "--project", str(project_dir)]
                )

        assert result.exit_code == 0
        assert "events" in result.output.lower() or "Sessions" in result.output

    def test_dry_run_with_yes_flag_runs_without_a_tty(self, memory_db: Database) -> None:
        """--yes combined with --dry-run must never block on a confirmation
        prompt — required for non-interactive/automated re-extract."""
        from callmem.cli import main

        config = Config(sensitive_data={"enabled": False, "llm_scan": False})
        engine = MemoryEngine(memory_db, config)
        engine.start_session()
        engine.ingest_one("note", "test event")

        runner = CliRunner()
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            from callmem.core.database import Database

            project_dir = Path(tmpdir)
            callmem_dir = project_dir / ".callmem"
            callmem_dir.mkdir()
            db = Database(callmem_dir / "memory.db")
            db.initialize()

            cfg = Config(sensitive_data={"enabled": False, "llm_scan": False})
            eng = MemoryEngine(db, cfg)
            eng.start_session()
            eng.ingest_one("note", "test event")

            config_path = callmem_dir / "config.toml"
            config_path.write_text(
                '[project]\nname = "test"\n[llm]\nbackend = "ollama"\n[ollama]\nmodel = "test"\n'
            )

            with patch("callmem.core.engine._create_llm_client") as mock_create:
                mock_llm = MagicMock()
                mock_llm.is_available.return_value = True
                mock_llm._generate.return_value = '{"decisions":[],"todos":[]}'
                mock_create.return_value = mock_llm

                result = runner.invoke(
                    main,
                    [
                        "re-extract", "--dry-run", "--yes",
                        "--project", str(project_dir),
                    ],
                    input="",
                )

        assert result.exit_code == 0
        assert "Cancelled" not in result.output

    def test_yes_flag_skips_confirmation_prompt(self, memory_db: Database) -> None:
        """Without --yes, the CLI reads a confirmation from stdin; with no
        input available that would abort. --yes must bypass that entirely
        for a real (non-dry-run) re-extract."""
        from callmem.cli import main

        runner = CliRunner()
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            from callmem.core.database import Database

            project_dir = Path(tmpdir)
            callmem_dir = project_dir / ".callmem"
            callmem_dir.mkdir()
            db = Database(callmem_dir / "memory.db")
            db.initialize()

            cfg = Config(
                project={"name": "test"},
                sensitive_data={"enabled": False, "llm_scan": False},
            )
            eng = MemoryEngine(db, cfg)
            eng.start_session()
            eng.ingest_one("note", "test event")

            config_path = callmem_dir / "config.toml"
            config_path.write_text(
                '[project]\nname = "test"\n[llm]\nbackend = "ollama"\n[ollama]\nmodel = "test"\n'
            )

            with patch("callmem.core.engine._create_llm_client") as mock_create:
                mock_llm = MagicMock()
                mock_llm.is_available.return_value = True
                mock_llm._generate.return_value = '{"decisions":[],"todos":[]}'
                mock_create.return_value = mock_llm

                result = runner.invoke(
                    main,
                    ["re-extract", "--yes", "--project", str(project_dir)],
                    input="",
                )

        assert result.exit_code == 0
        assert "Cancelled" not in result.output
        assert "Re-extraction complete" in result.output

    def test_failed_batch_prints_summary_and_exits_nonzero(
        self, memory_db: Database
    ) -> None:
        """End-of-run must report failed batches and exit non-zero so
        automation notices a partial re-extraction (phase0-reliability
        task 6)."""
        from callmem.cli import main

        runner = CliRunner()
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            from callmem.core.database import Database

            project_dir = Path(tmpdir)
            callmem_dir = project_dir / ".callmem"
            callmem_dir.mkdir()
            db = Database(callmem_dir / "memory.db")
            db.initialize()

            cfg = Config(
                project={"name": "test"},
                sensitive_data={"enabled": False, "llm_scan": False},
            )
            eng = MemoryEngine(db, cfg)
            eng.start_session()
            eng.ingest_one("note", "test event")

            config_path = callmem_dir / "config.toml"
            config_path.write_text(
                '[project]\nname = "test"\n[llm]\nbackend = "ollama"\n[ollama]\nmodel = "test"\n'
            )

            with patch("callmem.core.engine._create_llm_client") as mock_create:
                mock_llm = MagicMock()
                mock_llm.is_available.return_value = True
                mock_llm._generate.return_value = None
                mock_create.return_value = mock_llm

                result = runner.invoke(
                    main,
                    ["re-extract", "--yes", "--project", str(project_dir)],
                    input="",
                )

        assert result.exit_code != 0
        assert "failed" in result.output.lower()

    def test_no_db_shows_error(self) -> None:
        from callmem.cli import main

        runner = CliRunner()
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            result = runner.invoke(
                main, ["re-extract", "--project", tmpdir]
            )
        assert "No callmem database" in result.output


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
