"""Tests for CLI commands."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import patch

from click.testing import CliRunner

if TYPE_CHECKING:
    from pathlib import Path

from callmem.cli import main


class TestHelp:
    def test_help_shows_commands(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["--help"])
        assert result.exit_code == 0
        assert "init" in result.output
        assert "serve" in result.output
        assert "ui" in result.output
        assert "status" in result.output

    def test_version_flag(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["--version"])
        assert result.exit_code == 0
        assert "callmem" in result.output

    def test_init_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["init", "--help"])
        assert result.exit_code == 0
        assert "--project" in result.output


class TestInit:
    def test_creates_directory_and_files(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["init", "--project", str(tmp_path)])
        assert result.exit_code == 0
        assert (tmp_path / ".callmem").is_dir()
        assert (tmp_path / ".callmem" / "memory.db").exists()
        assert (tmp_path / ".callmem" / "config.toml").exists()

    def test_config_toml_content(self, tmp_path: Path) -> None:
        runner = CliRunner()
        runner.invoke(main, ["init", "--project", str(tmp_path)])
        config_text = (tmp_path / ".callmem" / "config.toml").read_text()
        assert "qwen3:8b" in config_text
        assert tmp_path.name in config_text

    def test_database_initialized(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["init", "--project", str(tmp_path)])
        assert "Schema:   v22" in result.output

    def test_idempotent(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result1 = runner.invoke(main, ["init", "--project", str(tmp_path)])
        assert result1.exit_code == 0
        result2 = runner.invoke(main, ["init", "--project", str(tmp_path)])
        assert result2.exit_code == 0
        assert (tmp_path / ".callmem" / "memory.db").exists()

    def test_config_not_overwritten(self, tmp_path: Path) -> None:
        runner = CliRunner()
        runner.invoke(main, ["init", "--project", str(tmp_path)])
        config_path = tmp_path / ".callmem" / "config.toml"
        original = config_path.read_text()
        runner.invoke(main, ["init", "--project", str(tmp_path)])
        assert config_path.read_text() == original


class TestServe:
    def test_serve_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["serve", "--help"])
        assert result.exit_code == 0
        assert "--project" in result.output
        assert "--transport" in result.output


class TestUI:
    def test_ui_outputs_url(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        runner = CliRunner()
        runner.invoke(main, ["init", "--project", str(tmp_path)])
        with patch("uvicorn.run"):
            result = runner.invoke(main, ["ui", "--project", str(tmp_path)])
        assert "http://0.0.0.0:9090" in result.output

    def test_ui_custom_port(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        runner = CliRunner()
        runner.invoke(main, ["init", "--project", str(tmp_path)])
        with patch("uvicorn.run"):
            result = runner.invoke(
                main, ["ui", "--project", str(tmp_path), "--port", "8080"]
            )
        assert "8080" in result.output


class TestStatus:
    def test_status_empty_database(self, tmp_path: Path) -> None:
        runner = CliRunner()
        runner.invoke(main, ["init", "--project", str(tmp_path)])
        result = runner.invoke(main, ["status", "--project", str(tmp_path)])
        assert result.exit_code == 0
        assert "Events:       0" in result.output
        assert "Schema:       v22" in result.output

    def test_status_no_database(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["status", "--project", str(tmp_path)])
        assert "No callmem database found" in result.output

    def test_status_shows_project_path(self, tmp_path: Path) -> None:
        runner = CliRunner()
        runner.invoke(main, ["init", "--project", str(tmp_path)])
        result = runner.invoke(main, ["status", "--project", str(tmp_path)])
        assert str(tmp_path) in result.output


class TestRequeueFailed:
    def _seed_failed_job(
        self, tmp_path: Path, job_type: str = "extract_entities"
    ) -> None:
        import sqlite3

        db_path = tmp_path / ".callmem" / "memory.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO jobs (id, type, payload, status, attempts, max_attempts, "
            "created_at) VALUES (?, ?, '{}', 'failed', 1, 1, datetime('now'))",
            (f"job-{job_type}", job_type),
        )
        conn.commit()
        conn.close()

    def test_requeues_failed_jobs_and_reports_count(self, tmp_path: Path) -> None:
        runner = CliRunner()
        runner.invoke(main, ["init", "--project", str(tmp_path)])
        self._seed_failed_job(tmp_path)

        result = runner.invoke(
            main, ["requeue-failed", "--project", str(tmp_path)]
        )
        assert result.exit_code == 0
        assert "1" in result.output

        import sqlite3

        conn = sqlite3.connect(str(tmp_path / ".callmem" / "memory.db"))
        row = conn.execute(
            "SELECT status, attempts, next_attempt_at FROM jobs WHERE id = ?",
            ("job-extract_entities",),
        ).fetchone()
        conn.close()
        assert row == ("pending", 0, None)

    def test_type_filter(self, tmp_path: Path) -> None:
        runner = CliRunner()
        runner.invoke(main, ["init", "--project", str(tmp_path)])
        self._seed_failed_job(tmp_path, "extract_entities")
        self._seed_failed_job(tmp_path, "generate_summary")

        result = runner.invoke(
            main,
            [
                "requeue-failed", "--project", str(tmp_path),
                "--type", "extract_entities",
            ],
        )
        assert result.exit_code == 0
        assert "1" in result.output

        import sqlite3

        conn = sqlite3.connect(str(tmp_path / ".callmem" / "memory.db"))
        statuses = dict(
            conn.execute("SELECT id, status FROM jobs").fetchall()
        )
        conn.close()
        assert statuses["job-extract_entities"] == "pending"
        assert statuses["job-generate_summary"] == "failed"

    def test_no_db_shows_error(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(
            main, ["requeue-failed", "--project", str(tmp_path)]
        )
        assert "No callmem database" in result.output


class TestClearFailed:
    def _seed_failed_job(
        self, tmp_path: Path, job_type: str = "extract_entities"
    ) -> None:
        import sqlite3

        db_path = tmp_path / ".callmem" / "memory.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO jobs (id, type, payload, status, attempts, max_attempts, "
            "created_at) VALUES (?, ?, '{}', 'failed', 1, 1, datetime('now'))",
            (f"job-{job_type}", job_type),
        )
        conn.commit()
        conn.close()

    def test_prompts_for_confirmation_and_clears_on_yes(
        self, tmp_path: Path
    ) -> None:
        runner = CliRunner()
        runner.invoke(main, ["init", "--project", str(tmp_path)])
        self._seed_failed_job(tmp_path)

        result = runner.invoke(
            main, ["clear-failed", "--project", str(tmp_path)], input="y\n"
        )
        assert result.exit_code == 0
        assert "1" in result.output
        assert "Cleared 1 failed job(s)." in result.output

        import sqlite3

        conn = sqlite3.connect(str(tmp_path / ".callmem" / "memory.db"))
        row = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE id = ?", ("job-extract_entities",)
        ).fetchone()
        conn.close()
        assert row[0] == 0

    def test_declining_confirmation_cancels(self, tmp_path: Path) -> None:
        runner = CliRunner()
        runner.invoke(main, ["init", "--project", str(tmp_path)])
        self._seed_failed_job(tmp_path)

        result = runner.invoke(
            main, ["clear-failed", "--project", str(tmp_path)], input="n\n"
        )
        assert result.exit_code == 0
        assert "Cancelled" in result.output

        import sqlite3

        conn = sqlite3.connect(str(tmp_path / ".callmem" / "memory.db"))
        row = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE id = ?", ("job-extract_entities",)
        ).fetchone()
        conn.close()
        assert row[0] == 1

    def test_yes_flag_skips_confirmation_prompt(self, tmp_path: Path) -> None:
        runner = CliRunner()
        runner.invoke(main, ["init", "--project", str(tmp_path)])
        self._seed_failed_job(tmp_path)

        result = runner.invoke(
            main,
            ["clear-failed", "--project", str(tmp_path), "--yes"],
            input="",
        )
        assert result.exit_code == 0
        assert "Cancelled" not in result.output
        assert "Cleared 1 failed job(s)." in result.output

    def test_type_filter(self, tmp_path: Path) -> None:
        runner = CliRunner()
        runner.invoke(main, ["init", "--project", str(tmp_path)])
        self._seed_failed_job(tmp_path, "extract_entities")
        self._seed_failed_job(tmp_path, "generate_summary")

        result = runner.invoke(
            main,
            [
                "clear-failed", "--project", str(tmp_path),
                "--type", "extract_entities", "--yes",
            ],
        )
        assert result.exit_code == 0
        assert "Cleared 1 failed job(s)." in result.output

        import sqlite3

        conn = sqlite3.connect(str(tmp_path / ".callmem" / "memory.db"))
        statuses = dict(
            conn.execute("SELECT id, status FROM jobs").fetchall()
        )
        conn.close()
        assert "job-extract_entities" not in statuses
        assert statuses["job-generate_summary"] == "failed"

    def test_zero_failed_jobs_prints_message_and_exits_zero(
        self, tmp_path: Path
    ) -> None:
        runner = CliRunner()
        runner.invoke(main, ["init", "--project", str(tmp_path)])

        result = runner.invoke(
            main, ["clear-failed", "--project", str(tmp_path)]
        )
        assert result.exit_code == 0
        assert "No failed jobs to clear." in result.output

    def test_no_db_shows_error(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(
            main, ["clear-failed", "--project", str(tmp_path)]
        )
        assert "No callmem database" in result.output


class TestUnarchiveProtected:
    def _seed_archived_failure(self, tmp_path: Path) -> str:
        """Seed one archived, still-open failure entity via the same
        project resolution the command uses, so it lands in the same
        project row. Returns the entity id."""
        import sqlite3

        from callmem.core.config import load_config
        from callmem.core.database import Database
        from callmem.core.engine import MemoryEngine

        db_path = tmp_path / ".callmem" / "memory.db"
        config = load_config(tmp_path)
        db = Database(db_path)
        db.initialize()
        engine = MemoryEngine(db, config)
        project_id = engine.project_id

        entity_id = "en-failure-1"
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(
                "INSERT INTO entities (id, project_id, type, title, "
                "content, status, pinned, created_at, updated_at, "
                "archived_at) VALUES (?, ?, 'failure', 'open failure', "
                "'c', 'unresolved', 0, datetime('now'), datetime('now'), "
                "'2026-06-01T00:00:00+00:00')",
                (entity_id, project_id),
            )
            conn.commit()
        finally:
            conn.close()
        return entity_id

    def test_dry_run_default_prints_candidate_and_does_not_write(
        self, tmp_path: Path,
    ) -> None:
        import sqlite3

        runner = CliRunner()
        runner.invoke(main, ["init", "--project", str(tmp_path)])
        entity_id = self._seed_archived_failure(tmp_path)

        result = runner.invoke(
            main, ["unarchive-protected", "--project", str(tmp_path)]
        )
        assert result.exit_code == 0
        assert entity_id in result.output
        assert "Dry-run only" in result.output

        conn = sqlite3.connect(str(tmp_path / ".callmem" / "memory.db"))
        row = conn.execute(
            "SELECT archived_at FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()
        conn.close()
        assert row[0] is not None

    def test_yes_flag_restores_entity(self, tmp_path: Path) -> None:
        import sqlite3

        runner = CliRunner()
        runner.invoke(main, ["init", "--project", str(tmp_path)])
        entity_id = self._seed_archived_failure(tmp_path)

        result = runner.invoke(
            main,
            ["unarchive-protected", "--project", str(tmp_path), "--yes"],
        )
        assert result.exit_code == 0
        assert "Restored 1 entity(ies)." in result.output

        conn = sqlite3.connect(str(tmp_path / ".callmem" / "memory.db"))
        row = conn.execute(
            "SELECT archived_at FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()
        conn.close()
        assert row[0] is None

    def test_no_candidates_prints_message(self, tmp_path: Path) -> None:
        runner = CliRunner()
        runner.invoke(main, ["init", "--project", str(tmp_path)])

        result = runner.invoke(
            main, ["unarchive-protected", "--project", str(tmp_path)]
        )
        assert result.exit_code == 0
        assert "No archived-but-protected entities found." in result.output

    def test_no_db_shows_error(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(
            main, ["unarchive-protected", "--project", str(tmp_path)]
        )
        assert "No callmem database" in result.output

    def test_malformed_since_rejected_with_clear_message(
        self, tmp_path: Path,
    ) -> None:
        runner = CliRunner()
        runner.invoke(main, ["init", "--project", str(tmp_path)])
        entity_id = self._seed_archived_failure(tmp_path)

        result = runner.invoke(
            main,
            [
                "unarchive-protected", "--project", str(tmp_path),
                "--since", "not-a-date",
            ],
        )
        assert result.exit_code != 0
        assert "--since" in result.output

        import sqlite3

        conn = sqlite3.connect(str(tmp_path / ".callmem" / "memory.db"))
        row = conn.execute(
            "SELECT archived_at FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()
        conn.close()
        assert row[0] is not None


class TestAudit:
    def test_clean_db_passes(self, tmp_path: Path) -> None:
        runner = CliRunner()
        runner.invoke(main, ["init", "--project", str(tmp_path)])
        result = runner.invoke(main, ["audit", "--project", str(tmp_path)])
        assert result.exit_code == 0
        assert "no integrity issues" in result.output.lower()
        assert "Cross-project entity/event mismatches: 0" in result.output

    def test_missing_database_errors(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["audit", "--project", str(tmp_path)])
        assert result.exit_code == 1
        assert "No callmem database found" in result.output

    def test_cross_project_contamination_fails(self, tmp_path: Path) -> None:
        """Seed contamination directly to verify the auditor catches it."""
        import sqlite3

        runner = CliRunner()
        runner.invoke(main, ["init", "--project", str(tmp_path)])
        db_path = tmp_path / ".callmem" / "memory.db"

        # Two projects; one event in project A; one entity claiming
        # project B but linked to the project-A event.
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(
                "INSERT INTO projects (id, name, created_at, updated_at) VALUES "
                "('pA', 'alpha', datetime('now'), datetime('now')), "
                "('pB', 'beta',  datetime('now'), datetime('now'))",
            )
            conn.execute(
                "INSERT INTO sessions (id, project_id, started_at, status) "
                "VALUES ('sA', 'pA', datetime('now'), 'active')",
            )
            conn.execute(
                "INSERT INTO events (id, session_id, project_id, type, "
                "content, timestamp) "
                "VALUES ('ev1', 'sA', 'pA', 'note', 'alpha event', "
                "datetime('now'))",
            )
            conn.execute(
                "INSERT INTO entities (id, project_id, source_event_id, "
                "type, title, content, pinned, created_at, updated_at) "
                "VALUES ('en1', 'pB', 'ev1', 'note', 'contaminated', "
                "'contaminated', 0, datetime('now'), datetime('now'))",
            )
            conn.commit()
        finally:
            conn.close()

        result = runner.invoke(main, ["audit", "--project", str(tmp_path)])
        assert result.exit_code == 2
        assert "Cross-project entity/event mismatches: 1" in result.output
        assert "integrity issue" in result.output.lower()

    def test_dangling_event_ref_fails(self, tmp_path: Path) -> None:
        import sqlite3

        runner = CliRunner()
        runner.invoke(main, ["init", "--project", str(tmp_path)])
        db_path = tmp_path / ".callmem" / "memory.db"

        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(
                "INSERT INTO projects (id, name, created_at, updated_at) "
                "VALUES ('pA', 'alpha', datetime('now'), datetime('now'))",
            )
            # Entity refers to an event that doesn't exist
            conn.execute(
                "INSERT INTO entities (id, project_id, source_event_id, "
                "type, title, content, pinned, created_at, updated_at) "
                "VALUES ('en1', 'pA', 'ghost', 'note', 'dangling', "
                "'dangling', 0, datetime('now'), datetime('now'))",
            )
            conn.commit()
        finally:
            conn.close()

        result = runner.invoke(main, ["audit", "--project", str(tmp_path)])
        assert result.exit_code == 2
        assert "dangling source_event_id: 1" in result.output


class TestVacuum:
    def test_reports_size_before_and_after(self, tmp_path: Path) -> None:
        runner = CliRunner()
        runner.invoke(main, ["init", "--project", str(tmp_path)])
        result = runner.invoke(main, ["vacuum", "--project", str(tmp_path)])
        assert result.exit_code == 0
        assert "VACUUM complete" in result.output
        assert "reclaimed" in result.output

    def test_missing_database_errors(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["vacuum", "--project", str(tmp_path)])
        assert result.exit_code == 1
        assert "No callmem database found" in result.output

    def test_reclaims_space_after_deletion(self, tmp_path: Path) -> None:
        import sqlite3

        runner = CliRunner()
        runner.invoke(main, ["init", "--project", str(tmp_path)])
        db_path = tmp_path / ".callmem" / "memory.db"

        # Inflate the DB, then delete rows — SQLite will not shrink
        # the file on its own; VACUUM should reclaim the pages.
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS _bloat "
                "(id INTEGER PRIMARY KEY, blob TEXT)"
            )
            conn.executemany(
                "INSERT INTO _bloat (blob) VALUES (?)",
                [("x" * 5000,) for _ in range(500)],
            )
            conn.commit()
            inflated = db_path.stat().st_size
            conn.execute("DELETE FROM _bloat")
            conn.commit()
        finally:
            conn.close()

        before_vacuum = db_path.stat().st_size
        assert before_vacuum >= inflated - 100  # file still holds free pages

        result = runner.invoke(main, ["vacuum", "--project", str(tmp_path)])
        assert result.exit_code == 0
        after_vacuum = db_path.stat().st_size
        assert after_vacuum < before_vacuum


class TestEnsureAgentsMcpBlock:
    def test_appends_mcp_block_to_existing_agents(self, tmp_path: Path) -> None:
        agents = tmp_path / "AGENTS.md"
        agents.write_text("# My Project\n\nSome coding norms.\n")

        from callmem.cli import _ensure_agents_mcp_block
        _ensure_agents_mcp_block(agents)

        content = agents.read_text()
        assert "## Memory (callmem)" in content
        assert "mem_ingest" in content
        assert "mem_session_start" in content

    def test_does_not_duplicate_if_already_present(self, tmp_path: Path) -> None:
        agents = tmp_path / "AGENTS.md"
        agents.write_text("# My Project\n\nSome norms.\n\n## Memory (callmem)\n\nAlready here.\n")

        from callmem.cli import _ensure_agents_mcp_block
        _ensure_agents_mcp_block(agents)

        content = agents.read_text()
        assert content.count("## Memory (callmem)") == 1

    def test_no_op_if_mem_ingest_sentinel_found(self, tmp_path: Path) -> None:
        agents = tmp_path / "AGENTS.md"
        original = "# My Project\n\nCall mem_ingest to record events.\n"
        agents.write_text(original)

        from callmem.cli import _ensure_agents_mcp_block
        _ensure_agents_mcp_block(agents)

        assert agents.read_text() == original

    def test_appends_to_agents_with_old_snippet(self, tmp_path: Path) -> None:
        agents = tmp_path / "AGENTS.md"
        agents.write_text("# My Project\n\n## Startup briefing\n\nRead SESSION_SUMMARY.md.\n")

        from callmem.cli import _ensure_agents_mcp_block
        _ensure_agents_mcp_block(agents)

        content = agents.read_text()
        assert "## Startup briefing" in content
        assert "## Memory (callmem)" in content

    def test_no_agents_md_is_no_op(self, tmp_path: Path) -> None:
        from callmem.cli import _ensure_agents_mcp_block
        _ensure_agents_mcp_block(tmp_path / "nonexistent.md")

    def test_init_with_existing_agents_patches_mcp(self, tmp_path: Path) -> None:
        (tmp_path / "AGENTS.md").write_text("# Coding Norms\n\nBe excellent.\n")

        runner = CliRunner()
        result = runner.invoke(main, ["init", "--project", str(tmp_path)])
        assert result.exit_code == 0

        content = (tmp_path / "AGENTS.md").read_text()
        assert "## Memory (callmem)" in content

    def test_init_no_agents_writes_full_template(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["init", "--project", str(tmp_path)])
        assert result.exit_code == 0

        agents = tmp_path / "AGENTS.md"
        assert agents.exists()
        content = agents.read_text()
        assert "mem_ingest" in content

    def test_init_idempotent_mcp_block(self, tmp_path: Path) -> None:
        (tmp_path / "AGENTS.md").write_text("# Coding Norms\n\nBe excellent.\n")

        runner = CliRunner()
        runner.invoke(main, ["init", "--project", str(tmp_path)])
        content_after_first = (tmp_path / "AGENTS.md").read_text()

        runner.invoke(main, ["init", "--project", str(tmp_path)])
        content_after_second = (tmp_path / "AGENTS.md").read_text()

        assert content_after_first == content_after_second


class TestDedupe:
    """Consolidation is the evidence-based curator when enabled; the
    cruder title-similarity dedupe command must defer to it (see
    docs/plans/consolidation-enable.md Task 1)."""

    def _write_consolidation_config(self, tmp_path: Path, enabled: bool) -> None:
        config_path = tmp_path / ".callmem" / "config.toml"
        config_path.write_text(
            f"[consolidation]\nenabled = {'true' if enabled else 'false'}\n"
        )

    def _seed_duplicate_entities(self, tmp_path: Path) -> str:
        """Seed a near-duplicate pair (title-similarity ~0.94) that
        find_clusters will merge. Returns the loser id (the newer one)."""
        import sqlite3

        from callmem.core.config import load_config
        from callmem.core.database import Database
        from callmem.core.engine import MemoryEngine

        db_path = tmp_path / ".callmem" / "memory.db"
        config = load_config(tmp_path)
        db = Database(db_path)
        db.initialize()
        engine = MemoryEngine(db, config)
        project_id = engine.project_id

        rows = [
            ("en-dup-survivor", "fix flaky websocket reconnect loop",
             "2026-01-01T00:00:00+00:00"),
            ("en-dup-loser", "fix flaky websocket reconnect loop bug",
             "2026-01-02T00:00:00+00:00"),
        ]
        conn = sqlite3.connect(str(db_path))
        try:
            for eid, title, created in rows:
                conn.execute(
                    "INSERT INTO entities (id, project_id, type, title, "
                    "content, status, pinned, stale, created_at, updated_at) "
                    "VALUES (?, ?, 'failure', ?, 'c', 'unresolved', 0, 0, "
                    "?, ?)",
                    (eid, project_id, title, created, created),
                )
            conn.commit()
        finally:
            conn.close()
        return "en-dup-loser"

    def _stale_flag(self, tmp_path: Path, entity_id: str) -> int:
        import sqlite3

        conn = sqlite3.connect(str(tmp_path / ".callmem" / "memory.db"))
        try:
            row = conn.execute(
                "SELECT stale FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
        finally:
            conn.close()
        return row[0]

    def test_consolidation_enabled_without_force_refuses(
        self, tmp_path: Path,
    ) -> None:
        runner = CliRunner()
        runner.invoke(main, ["init", "--project", str(tmp_path)])
        self._write_consolidation_config(tmp_path, enabled=True)
        loser_id = self._seed_duplicate_entities(tmp_path)

        result = runner.invoke(main, ["dedupe", "--project", str(tmp_path)])

        assert result.exit_code != 0
        assert "consolidation" in result.output.lower()
        assert "--force" in result.output
        assert self._stale_flag(tmp_path, loser_id) == 0

    def test_consolidation_enabled_with_force_proceeds(
        self, tmp_path: Path,
    ) -> None:
        runner = CliRunner()
        runner.invoke(main, ["init", "--project", str(tmp_path)])
        self._write_consolidation_config(tmp_path, enabled=True)
        loser_id = self._seed_duplicate_entities(tmp_path)

        result = runner.invoke(
            main, ["dedupe", "--project", str(tmp_path), "--force"],
        )

        assert result.exit_code == 0
        assert self._stale_flag(tmp_path, loser_id) == 1

    def test_consolidation_disabled_unchanged_behaviour(
        self, tmp_path: Path,
    ) -> None:
        runner = CliRunner()
        runner.invoke(main, ["init", "--project", str(tmp_path)])
        self._write_consolidation_config(tmp_path, enabled=False)
        loser_id = self._seed_duplicate_entities(tmp_path)

        result = runner.invoke(main, ["dedupe", "--project", str(tmp_path)])

        assert result.exit_code == 0
        assert self._stale_flag(tmp_path, loser_id) == 1

    def test_dry_run_never_blocked_even_when_consolidation_enabled(
        self, tmp_path: Path,
    ) -> None:
        runner = CliRunner()
        runner.invoke(main, ["init", "--project", str(tmp_path)])
        self._write_consolidation_config(tmp_path, enabled=True)
        loser_id = self._seed_duplicate_entities(tmp_path)

        result = runner.invoke(
            main, ["dedupe", "--project", str(tmp_path), "--dry-run"],
        )

        assert result.exit_code == 0
        assert "Would mark" in result.output
        assert self._stale_flag(tmp_path, loser_id) == 0


class TestConsolidateDryRun:
    """CLI surface for Task 4's calibration harness (see
    docs/plans/consolidation-enable.md Task 4): ``callmem consolidate
    --dry-run`` must never write, must honor --threshold/--limit, and
    must report the fail-open contract rather than a would-archive."""

    def _seed_entity(
        self, tmp_path: Path, eid: str, title: str, content: str,
        created: str = "2026-01-01T00:00:00+00:00",
    ) -> None:
        import sqlite3

        db_path = tmp_path / ".callmem" / "memory.db"
        from callmem.core.config import load_config
        from callmem.core.database import Database
        from callmem.core.engine import MemoryEngine

        config = load_config(tmp_path)
        db = Database(db_path)
        db.initialize()
        project_id = MemoryEngine(db, config).project_id

        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(
                "INSERT INTO entities (id, project_id, type, title, "
                "content, status, pinned, stale, created_at, updated_at) "
                "VALUES (?, ?, 'fact', ?, ?, NULL, 0, 0, ?, ?)",
                (eid, project_id, title, content, created, created),
            )
            conn.commit()
        finally:
            conn.close()

    def _entity_row(self, tmp_path: Path, eid: str) -> tuple:
        import sqlite3

        conn = sqlite3.connect(str(tmp_path / ".callmem" / "memory.db"))
        try:
            return conn.execute(
                "SELECT archived_at, stale, superseded_by FROM entities "
                "WHERE id = ?",
                (eid,),
            ).fetchone()
        finally:
            conn.close()

    def _stub_llm(self, response_by_new_id: dict[str, str] | None = None):
        """A fake `_create_llm_client` return value: `.extract(prompt)`
        returns a NOOP verdict for whichever new-entity id appears in
        the prompt, or None (fail-open) if `response_by_new_id` is None.
        """

        class _Stub:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def extract(self, prompt: str) -> str | None:
                self.calls.append(prompt)
                if response_by_new_id is None:
                    return None
                for new_id, verdict_json in response_by_new_id.items():
                    if new_id in prompt:
                        return verdict_json
                return None

        return _Stub()

    def test_requires_dry_run_flag(self, tmp_path: Path) -> None:
        runner = CliRunner()
        runner.invoke(main, ["init", "--project", str(tmp_path)])

        result = runner.invoke(main, ["consolidate", "--project", str(tmp_path)])

        assert result.exit_code != 0
        assert "--dry-run" in result.output

    def test_no_database_found(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(
            main, ["consolidate", "--project", str(tmp_path), "--dry-run"],
        )
        assert result.exit_code != 0
        assert "No callmem database found" in result.output

    def test_no_llm_backend_configured_refuses(self, tmp_path: Path) -> None:
        runner = CliRunner()
        runner.invoke(main, ["init", "--project", str(tmp_path)])
        self._seed_entity(tmp_path, "e1", "Auth uses JWT", "RS256")

        with patch("callmem.core.engine._create_llm_client", return_value=None):
            result = runner.invoke(
                main, ["consolidate", "--project", str(tmp_path), "--dry-run"],
            )

        assert result.exit_code != 0
        assert "No LLM backend" in result.output

    def test_dry_run_writes_nothing_and_reports_noop(
        self, tmp_path: Path,
    ) -> None:
        runner = CliRunner()
        runner.invoke(main, ["init", "--project", str(tmp_path)])
        self._seed_entity(
            tmp_path, "e1", "Auth uses JWT", "RS256 tokens",
            created="2026-01-01T00:00:00+00:00",
        )
        self._seed_entity(
            tmp_path, "e2", "Auth uses JWT", "RS256 tokens",
            created="2026-01-02T00:00:00+00:00",
        )
        before_e1 = self._entity_row(tmp_path, "e1")
        before_e2 = self._entity_row(tmp_path, "e2")

        response = json.dumps([{
            "new_id": "e2", "verdict": "NOOP", "existing_id": "e1",
            "reason": "same fact restated",
        }])
        stub = self._stub_llm({"e2": response})

        with patch("callmem.core.engine._create_llm_client", return_value=stub):
            result = runner.invoke(
                main,
                ["consolidate", "--project", str(tmp_path), "--dry-run",
                 "--threshold", "0.3"],
            )

        assert result.exit_code == 0, result.output
        assert "NOOP" in result.output
        assert "same fact restated" in result.output
        # Writes nothing: both rows byte-identical to before the run.
        assert self._entity_row(tmp_path, "e1") == before_e1
        assert self._entity_row(tmp_path, "e2") == before_e2

    def test_limit_bounds_judge_calls_and_reports_skipped(
        self, tmp_path: Path,
    ) -> None:
        runner = CliRunner()
        runner.invoke(main, ["init", "--project", str(tmp_path)])
        # Three near-identical entities -- with --limit 1, only one may
        # ever be judged/considered, and the other two must be reported
        # as skipped rather than silently dropped.
        for i, created in enumerate([
            "2026-01-03T00:00:00+00:00",
            "2026-01-02T00:00:00+00:00",
            "2026-01-01T00:00:00+00:00",
        ]):
            self._seed_entity(
                tmp_path, f"e{i}", "Auth uses JWT", "RS256 tokens",
                created=created,
            )
        stub = self._stub_llm(None)  # fail-open on every call

        with patch("callmem.core.engine._create_llm_client", return_value=stub):
            result = runner.invoke(
                main,
                ["consolidate", "--project", str(tmp_path), "--dry-run",
                 "--threshold", "0.3", "--limit", "1"],
            )

        assert result.exit_code == 0, result.output
        assert "1 of 3" in result.output
        assert "2 skipped" in result.output
        # At most one entity was ever in a batch -> at most one judge
        # call (its only possible candidate was skipped, so likely
        # zero, but never more than one).
        assert len(stub.calls) <= 1

    def test_threshold_override_changes_qualifying_set(
        self, tmp_path: Path,
    ) -> None:
        runner = CliRunner()
        runner.invoke(main, ["init", "--project", str(tmp_path)])
        # e2's title tokens are a subset of e1's title tokens, so the
        # FTS AND-match finds e1 directly (no reliance on the OR-retry
        # fallback, which only fires when the AND query matches nothing
        # at all -- since e2 is itself a persisted row here, as every
        # dry-run "new" entity is, an AND query over e2's own title
        # always matches e2 trivially and would otherwise mask whether
        # e1 was found too).
        self._seed_entity(
            tmp_path, "e1", "Auth uses JWT tokens for sessions",
            "RS256, rotated quarterly",
        )
        self._seed_entity(
            tmp_path, "e2", "Auth uses JWT tokens",
            "short form", created="2026-01-02T00:00:00+00:00",
        )

        strict_stub = self._stub_llm(None)
        with patch(
            "callmem.core.engine._create_llm_client", return_value=strict_stub,
        ):
            runner.invoke(
                main,
                ["consolidate", "--project", str(tmp_path), "--dry-run",
                 "--threshold", "0.99"],
            )
        assert strict_stub.calls == []

        loose_stub = self._stub_llm({
            "e2": json.dumps([{
                "new_id": "e2", "verdict": "ADD", "existing_id": None,
                "reason": "distinct enough",
            }]),
        })
        with patch(
            "callmem.core.engine._create_llm_client", return_value=loose_stub,
        ):
            result = runner.invoke(
                main,
                ["consolidate", "--project", str(tmp_path), "--dry-run",
                 "--threshold", "0.1"],
            )
        assert result.exit_code == 0, result.output
        assert len(loose_stub.calls) >= 1

    def test_judge_failure_reports_add_never_a_would_archive(
        self, tmp_path: Path,
    ) -> None:
        runner = CliRunner()
        runner.invoke(main, ["init", "--project", str(tmp_path)])
        self._seed_entity(
            tmp_path, "e1", "Auth uses JWT", "RS256 tokens",
            created="2026-01-01T00:00:00+00:00",
        )
        self._seed_entity(
            tmp_path, "e2", "Auth uses JWT", "RS256 tokens",
            created="2026-01-02T00:00:00+00:00",
        )
        before_e1 = self._entity_row(tmp_path, "e1")
        before_e2 = self._entity_row(tmp_path, "e2")
        stub = self._stub_llm(None)  # malformed/absent -> fail-open

        with patch("callmem.core.engine._create_llm_client", return_value=stub):
            result = runner.invoke(
                main,
                ["consolidate", "--project", str(tmp_path), "--dry-run",
                 "--threshold", "0.3"],
            )

        assert result.exit_code == 0, result.output
        assert "judge_failed" in result.output
        # No per-decision line reports an archive/supersede verdict --
        # only the always-printed "NOOP=0"/"UPDATE=0" summary tallies,
        # which is exactly the fail-open guarantee: never a would-archive.
        assert "] NOOP" not in result.output
        assert "] UPDATE" not in result.output
        assert "NOOP=0" in result.output
        assert "UPDATE=0" in result.output
        assert self._entity_row(tmp_path, "e1") == before_e1
        assert self._entity_row(tmp_path, "e2") == before_e2
