"""Tests for the `callmem resolve` CLI command's judged sweep.

Judged mode is the default when an LLM backend is configured; --no-judge
restores the legacy auto-close-on-keyword-match behavior. See
extraction.py's ``gather_resolution_candidates``/``resolve_judge.py`` for
the underlying logic -- these tests exercise CLI wiring: backend refusal,
--no-judge unchanged, dry-run rendering, and the trailing counts line.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from callmem.cli import main
from callmem.core.database import Database
from callmem.core.engine import MemoryEngine
from callmem.models.config import Config
from callmem.models.entities import Entity


def _insert_entity(
    db: Database, project_id: str, etype: str, title: str,
    status: str | None = None,
) -> str:
    entity = Entity(
        project_id=project_id, type=etype, title=title, content=title,
        status=status,
    )
    row = entity.to_row()
    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO entities "
            "(id, project_id, source_event_id, type, title, content, "
            "key_points, synopsis, status, priority, pinned, "
            "created_at, updated_at, resolved_at, metadata, archived_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                row["id"], row["project_id"], row["source_event_id"],
                row["type"], row["title"], row["content"],
                row["key_points"], row["synopsis"], row["status"],
                row["priority"], row["pinned"], row["created_at"],
                row["updated_at"], row["resolved_at"], row["metadata"],
                row["archived_at"],
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return entity.id


def _make_project(tmpdir: str) -> tuple[Path, Path, Database, str]:
    """Sets up a .callmem project dir with a real db and a matching
    config.toml naming the project "test" -- the CliRunner invocation
    below (a fresh process-in-process `resolve` call) reads config.toml
    from disk, so the db and the config's project name must agree."""
    project_dir = Path(tmpdir)
    callmem_dir = project_dir / ".callmem"
    callmem_dir.mkdir()
    db = Database(callmem_dir / "memory.db")
    db.initialize()

    cfg = Config(
        project={"name": "test"},
        sensitive_data={"enabled": False, "llm_scan": False},
    )
    engine = MemoryEngine(db, cfg)
    return project_dir, callmem_dir, db, engine.project_id


def _judge_response(verdict: str) -> str:
    return json.dumps([{"pair": 1, "verdict": verdict, "reason": "because"}])


class TestNoBackendRefusal:
    def test_refuses_judged_mode_with_no_backend_configured(self) -> None:
        runner = CliRunner()
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir, callmem_dir, db, project_id = _make_project(tmpdir)
            (callmem_dir / "config.toml").write_text(
                '[project]\nname = "test"\n[llm]\nbackend = "none"\n'
            )

            result = runner.invoke(
                main, ["resolve", "--project", str(project_dir)],
            )

        assert result.exit_code != 0
        assert "none" in result.output.lower()
        assert "--no-judge" in result.output

    def test_no_judge_flag_works_without_any_backend(self) -> None:
        """--no-judge must not require or check an LLM backend at all."""
        runner = CliRunner()
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir, callmem_dir, db, project_id = _make_project(tmpdir)
            (callmem_dir / "config.toml").write_text(
                '[project]\nname = "test"\n[llm]\nbackend = "none"\n'
            )

            result = runner.invoke(
                main, ["resolve", "--no-judge", "--project", str(project_dir)],
            )

        assert result.exit_code == 0


class TestNoJudgeLegacyUnchanged:
    def test_no_judge_closes_keyword_match_immediately(self) -> None:
        runner = CliRunner()
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir, callmem_dir, db, project_id = _make_project(tmpdir)
            (callmem_dir / "config.toml").write_text(
                '[project]\nname = "test"\n[llm]\nbackend = "none"\n'
            )
            _insert_entity(
                db, project_id, "feature",
                "Analysis history selector implemented",
            )
            todo_id = _insert_entity(
                db, project_id, "todo",
                "Implement analysis history selector", status="open",
            )

            result = runner.invoke(
                main, ["resolve", "--no-judge", "--project", str(project_dir)],
            )

            assert result.exit_code == 0
            assert "Closed 1 item" in result.output
            conn = db.connect()
            row = conn.execute(
                "SELECT status FROM entities WHERE id = ?", (todo_id,),
            ).fetchone()
            conn.close()
            assert row["status"] == "done"


class TestJudgedDefaultMode:
    def test_confirmed_verdict_closes_and_reports_counts(self) -> None:
        runner = CliRunner()
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir, callmem_dir, db, project_id = _make_project(tmpdir)
            (callmem_dir / "config.toml").write_text(
                '[project]\nname = "test"\n[llm]\nbackend = "ollama"\n'
                '[ollama]\nmodel = "test"\n'
            )
            _insert_entity(
                db, project_id, "feature",
                "Analysis history selector implemented",
            )
            todo_id = _insert_entity(
                db, project_id, "todo",
                "Implement analysis history selector", status="open",
            )

            with patch("callmem.core.engine._create_llm_client") as mock_create:
                mock_llm = MagicMock()
                mock_llm.extract.return_value = _judge_response("CONFIRMED")
                mock_create.return_value = mock_llm

                result = runner.invoke(
                    main, ["resolve", "--project", str(project_dir)],
                )

            assert result.exit_code == 0
            assert "Closed 1 item" in result.output
            assert "confirmed-closed: 1" in result.output
            assert "contradicted-open: 0" in result.output
            assert "uncertain-open: 0" in result.output
            conn = db.connect()
            row = conn.execute(
                "SELECT status FROM entities WHERE id = ?", (todo_id,),
            ).fetchone()
            conn.close()
            assert row["status"] == "done"

    def test_contradicted_verdict_leaves_open_and_reports_counts(self) -> None:
        runner = CliRunner()
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir, callmem_dir, db, project_id = _make_project(tmpdir)
            (callmem_dir / "config.toml").write_text(
                '[project]\nname = "test"\n[llm]\nbackend = "ollama"\n'
                '[ollama]\nmodel = "test"\n'
            )
            _insert_entity(
                db, project_id, "feature",
                "Analysis history selector implemented",
            )
            todo_id = _insert_entity(
                db, project_id, "todo",
                "Implement analysis history selector", status="open",
            )

            with patch("callmem.core.engine._create_llm_client") as mock_create:
                mock_llm = MagicMock()
                mock_llm.extract.return_value = _judge_response("CONTRADICTED")
                mock_create.return_value = mock_llm

                result = runner.invoke(
                    main, ["resolve", "--project", str(project_dir)],
                )

            assert result.exit_code == 0
            assert "Nothing closed" in result.output
            assert "confirmed-closed: 0" in result.output
            assert "contradicted-open: 1" in result.output
            conn = db.connect()
            row = conn.execute(
                "SELECT status FROM entities WHERE id = ?", (todo_id,),
            ).fetchone()
            conn.close()
            assert row["status"] == "open"

    def test_dry_run_shows_per_pair_verdict_without_closing(self) -> None:
        runner = CliRunner()
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir, callmem_dir, db, project_id = _make_project(tmpdir)
            (callmem_dir / "config.toml").write_text(
                '[project]\nname = "test"\n[llm]\nbackend = "ollama"\n'
                '[ollama]\nmodel = "test"\n'
            )
            _insert_entity(
                db, project_id, "feature",
                "Analysis history selector implemented",
            )
            todo_id = _insert_entity(
                db, project_id, "todo",
                "Implement analysis history selector", status="open",
            )

            with patch("callmem.core.engine._create_llm_client") as mock_create:
                mock_llm = MagicMock()
                mock_llm.extract.return_value = _judge_response("CONFIRMED")
                mock_create.return_value = mock_llm

                result = runner.invoke(
                    main,
                    ["resolve", "--dry-run", "--project", str(project_dir)],
                )

            assert result.exit_code == 0
            assert "[CONFIRMED]" in result.output
            assert "confirmed-closed: 1" in result.output
            conn = db.connect()
            row = conn.execute(
                "SELECT status FROM entities WHERE id = ?", (todo_id,),
            ).fetchone()
            conn.close()
            assert row["status"] == "open"

    def test_malformed_judge_response_leaves_everything_uncertain(self) -> None:
        runner = CliRunner()
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir, callmem_dir, db, project_id = _make_project(tmpdir)
            (callmem_dir / "config.toml").write_text(
                '[project]\nname = "test"\n[llm]\nbackend = "ollama"\n'
                '[ollama]\nmodel = "test"\n'
            )
            _insert_entity(
                db, project_id, "feature",
                "Analysis history selector implemented",
            )
            todo_id = _insert_entity(
                db, project_id, "todo",
                "Implement analysis history selector", status="open",
            )

            with patch("callmem.core.engine._create_llm_client") as mock_create:
                mock_llm = MagicMock()
                mock_llm.extract.return_value = "not valid json"
                mock_create.return_value = mock_llm

                result = runner.invoke(
                    main, ["resolve", "--project", str(project_dir)],
                )

            assert result.exit_code == 0
            assert "uncertain-open: 1" in result.output
            assert "confirmed-closed: 0" in result.output
            conn = db.connect()
            row = conn.execute(
                "SELECT status FROM entities WHERE id = ?", (todo_id,),
            ).fetchone()
            conn.close()
            assert row["status"] == "open"

    def test_no_candidates_still_reports_zeroed_counts(self) -> None:
        runner = CliRunner()
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir, callmem_dir, db, project_id = _make_project(tmpdir)
            (callmem_dir / "config.toml").write_text(
                '[project]\nname = "test"\n[llm]\nbackend = "ollama"\n'
                '[ollama]\nmodel = "test"\n'
            )

            with patch("callmem.core.engine._create_llm_client") as mock_create:
                mock_llm = MagicMock()
                mock_create.return_value = mock_llm

                result = runner.invoke(
                    main, ["resolve", "--project", str(project_dir)],
                )

        assert result.exit_code == 0
        assert "confirmed-closed: 0" in result.output
        assert mock_llm.extract.call_count == 0

    def test_no_db_shows_error(self) -> None:
        runner = CliRunner()
        with tempfile.TemporaryDirectory() as tmpdir:
            result = runner.invoke(main, ["resolve", "--project", tmpdir])
        assert "No callmem database" in result.output
