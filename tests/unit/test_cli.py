"""Tests for CLI commands."""

from __future__ import annotations

from typing import TYPE_CHECKING

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
        assert "Schema:   v8" in result.output

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
        assert "Schema:       v8" in result.output

    def test_status_no_database(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["status", "--project", str(tmp_path)])
        assert "No callmem database found" in result.output

    def test_status_shows_project_path(self, tmp_path: Path) -> None:
        runner = CliRunner()
        runner.invoke(main, ["init", "--project", str(tmp_path)])
        result = runner.invoke(main, ["status", "--project", str(tmp_path)])
        assert str(tmp_path) in result.output


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
