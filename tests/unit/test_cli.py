"""Tests for CLI commands."""

from __future__ import annotations

from typing import TYPE_CHECKING

from click.testing import CliRunner

if TYPE_CHECKING:
    from pathlib import Path

from llm_mem.cli import main


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
        assert "llm-mem" in result.output

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
        assert (tmp_path / ".llm-mem").is_dir()
        assert (tmp_path / ".llm-mem" / "memory.db").exists()
        assert (tmp_path / ".llm-mem" / "config.toml").exists()

    def test_config_toml_content(self, tmp_path: Path) -> None:
        runner = CliRunner()
        runner.invoke(main, ["init", "--project", str(tmp_path)])
        config_text = (tmp_path / ".llm-mem" / "config.toml").read_text()
        assert "qwen3:8b" in config_text
        assert tmp_path.name in config_text

    def test_database_initialized(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["init", "--project", str(tmp_path)])
        assert "Schema:   v6" in result.output

    def test_idempotent(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result1 = runner.invoke(main, ["init", "--project", str(tmp_path)])
        assert result1.exit_code == 0
        result2 = runner.invoke(main, ["init", "--project", str(tmp_path)])
        assert result2.exit_code == 0
        assert (tmp_path / ".llm-mem" / "memory.db").exists()

    def test_config_not_overwritten(self, tmp_path: Path) -> None:
        runner = CliRunner()
        runner.invoke(main, ["init", "--project", str(tmp_path)])
        config_path = tmp_path / ".llm-mem" / "config.toml"
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
        assert "Schema:       v6" in result.output

    def test_status_no_database(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["status", "--project", str(tmp_path)])
        assert "No llm-mem database found" in result.output

    def test_status_shows_project_path(self, tmp_path: Path) -> None:
        runner = CliRunner()
        runner.invoke(main, ["init", "--project", str(tmp_path)])
        result = runner.invoke(main, ["status", "--project", str(tmp_path)])
        assert str(tmp_path) in result.output


class TestEnsureAgentsMcpBlock:
    def test_appends_mcp_block_to_existing_agents(self, tmp_path: Path) -> None:
        agents = tmp_path / "AGENTS.md"
        agents.write_text("# My Project\n\nSome coding norms.\n")

        from llm_mem.cli import _ensure_agents_mcp_block
        _ensure_agents_mcp_block(agents)

        content = agents.read_text()
        assert "## Memory (llm-mem)" in content
        assert "mem_ingest" in content
        assert "mem_session_start" in content

    def test_does_not_duplicate_if_already_present(self, tmp_path: Path) -> None:
        agents = tmp_path / "AGENTS.md"
        agents.write_text("# My Project\n\nSome norms.\n\n## Memory (llm-mem)\n\nAlready here.\n")

        from llm_mem.cli import _ensure_agents_mcp_block
        _ensure_agents_mcp_block(agents)

        content = agents.read_text()
        assert content.count("## Memory (llm-mem)") == 1

    def test_no_op_if_mem_ingest_sentinel_found(self, tmp_path: Path) -> None:
        agents = tmp_path / "AGENTS.md"
        original = "# My Project\n\nCall mem_ingest to record events.\n"
        agents.write_text(original)

        from llm_mem.cli import _ensure_agents_mcp_block
        _ensure_agents_mcp_block(agents)

        assert agents.read_text() == original

    def test_appends_to_agents_with_old_snippet(self, tmp_path: Path) -> None:
        agents = tmp_path / "AGENTS.md"
        agents.write_text("# My Project\n\n## Startup briefing\n\nRead SESSION_SUMMARY.md.\n")

        from llm_mem.cli import _ensure_agents_mcp_block
        _ensure_agents_mcp_block(agents)

        content = agents.read_text()
        assert "## Startup briefing" in content
        assert "## Memory (llm-mem)" in content

    def test_no_agents_md_is_no_op(self, tmp_path: Path) -> None:
        from llm_mem.cli import _ensure_agents_mcp_block
        _ensure_agents_mcp_block(tmp_path / "nonexistent.md")

    def test_init_with_existing_agents_patches_mcp(self, tmp_path: Path) -> None:
        (tmp_path / "AGENTS.md").write_text("# Coding Norms\n\nBe excellent.\n")

        runner = CliRunner()
        result = runner.invoke(main, ["init", "--project", str(tmp_path)])
        assert result.exit_code == 0

        content = (tmp_path / "AGENTS.md").read_text()
        assert "## Memory (llm-mem)" in content

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
