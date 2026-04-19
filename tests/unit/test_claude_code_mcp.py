"""Tests for Claude Code MCP integration (WO-36)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from click.testing import CliRunner

from llm_mem.cli import (
    _claude_md_is_separate_file,
    _ensure_claude_code_mcp,
    main,
)

if TYPE_CHECKING:
    from pathlib import Path


class TestEnsureClaudeCodeMcp:
    def test_creates_mcp_json_when_missing(self, tmp_path: Path) -> None:
        _ensure_claude_code_mcp(tmp_path)

        mcp_path = tmp_path / ".mcp.json"
        assert mcp_path.exists()

        data = json.loads(mcp_path.read_text())
        assert "mcpServers" in data
        entry = data["mcpServers"]["llm-mem"]
        assert isinstance(entry["command"], str)
        assert isinstance(entry["args"], list)
        assert "llm_mem.mcp.server" in entry["args"]

    def test_preserves_other_servers(self, tmp_path: Path) -> None:
        mcp_path = tmp_path / ".mcp.json"
        mcp_path.write_text(json.dumps({
            "mcpServers": {
                "other-tool": {"command": "node", "args": ["server.js"]},
            },
        }))

        _ensure_claude_code_mcp(tmp_path)

        data = json.loads(mcp_path.read_text())
        assert "other-tool" in data["mcpServers"]
        assert data["mcpServers"]["other-tool"]["command"] == "node"
        assert "llm-mem" in data["mcpServers"]

    def test_preserves_top_level_keys(self, tmp_path: Path) -> None:
        mcp_path = tmp_path / ".mcp.json"
        mcp_path.write_text(json.dumps({
            "customField": "keep me",
            "mcpServers": {},
        }))

        _ensure_claude_code_mcp(tmp_path)

        data = json.loads(mcp_path.read_text())
        assert data["customField"] == "keep me"
        assert "llm-mem" in data["mcpServers"]

    def test_idempotent(self, tmp_path: Path) -> None:
        _ensure_claude_code_mcp(tmp_path)
        first = (tmp_path / ".mcp.json").read_text()

        _ensure_claude_code_mcp(tmp_path)
        second = (tmp_path / ".mcp.json").read_text()

        assert first == second

    def test_args_is_split_from_command(self, tmp_path: Path) -> None:
        _ensure_claude_code_mcp(tmp_path)

        data = json.loads((tmp_path / ".mcp.json").read_text())
        entry = data["mcpServers"]["llm-mem"]
        # Claude Code schema: command is a string, not a list
        assert isinstance(entry["command"], str)
        assert entry["command"] not in entry["args"]

    def test_updates_stale_llm_mem_entry(self, tmp_path: Path) -> None:
        mcp_path = tmp_path / ".mcp.json"
        mcp_path.write_text(json.dumps({
            "mcpServers": {
                "llm-mem": {"command": "wrong", "args": ["old"]},
            },
        }))

        _ensure_claude_code_mcp(tmp_path)

        data = json.loads(mcp_path.read_text())
        entry = data["mcpServers"]["llm-mem"]
        assert entry["command"] != "wrong"
        assert "llm_mem.mcp.server" in entry["args"]

    def test_invalid_json_leaves_file_alone(self, tmp_path: Path) -> None:
        mcp_path = tmp_path / ".mcp.json"
        mcp_path.write_text("{ not valid json")

        _ensure_claude_code_mcp(tmp_path)

        # Original content untouched
        assert mcp_path.read_text() == "{ not valid json"


class TestClaudeMdSymlinkDetection:
    def test_separate_file_is_detected(self, tmp_path: Path) -> None:
        agents = tmp_path / "AGENTS.md"
        agents.write_text("# agents\n")
        claude = tmp_path / "CLAUDE.md"
        claude.write_text("# claude-specific\n")

        assert _claude_md_is_separate_file(claude, agents) is True

    def test_symlink_to_agents_is_not_separate(self, tmp_path: Path) -> None:
        agents = tmp_path / "AGENTS.md"
        agents.write_text("# agents\n")
        claude = tmp_path / "CLAUDE.md"
        claude.symlink_to(agents)

        assert _claude_md_is_separate_file(claude, agents) is False

    def test_missing_claude_md_returns_false(self, tmp_path: Path) -> None:
        agents = tmp_path / "AGENTS.md"
        agents.write_text("# agents\n")

        assert _claude_md_is_separate_file(tmp_path / "CLAUDE.md", agents) is False


class TestInitIntegration:
    def test_init_creates_mcp_json(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["init", "--project", str(tmp_path)])
        assert result.exit_code == 0

        mcp_path = tmp_path / ".mcp.json"
        assert mcp_path.exists()
        data = json.loads(mcp_path.read_text())
        assert "llm-mem" in data["mcpServers"]

    def test_init_preserves_existing_mcp_json(self, tmp_path: Path) -> None:
        mcp_path = tmp_path / ".mcp.json"
        mcp_path.write_text(json.dumps({
            "mcpServers": {
                "other": {"command": "node", "args": ["x.js"]},
            },
        }))

        runner = CliRunner()
        result = runner.invoke(main, ["init", "--project", str(tmp_path)])
        assert result.exit_code == 0

        data = json.loads(mcp_path.read_text())
        assert "other" in data["mcpServers"]
        assert "llm-mem" in data["mcpServers"]

    def test_init_patches_separate_claude_md(self, tmp_path: Path) -> None:
        (tmp_path / "AGENTS.md").write_text("# Agents\n")
        claude = tmp_path / "CLAUDE.md"
        claude.write_text("# Claude-specific instructions\n")

        runner = CliRunner()
        result = runner.invoke(main, ["init", "--project", str(tmp_path)])
        assert result.exit_code == 0

        assert "## Memory (llm-mem)" in claude.read_text()

    def test_init_skips_patch_when_claude_md_is_symlink(self, tmp_path: Path) -> None:
        agents = tmp_path / "AGENTS.md"
        agents.write_text("# Agents\n")
        claude = tmp_path / "CLAUDE.md"
        claude.symlink_to(agents)

        runner = CliRunner()
        result = runner.invoke(main, ["init", "--project", str(tmp_path)])
        assert result.exit_code == 0

        # The symlink target (AGENTS.md) got patched exactly once
        assert agents.read_text().count("## Memory (llm-mem)") == 1

    def test_init_idempotent_for_mcp_json(self, tmp_path: Path) -> None:
        runner = CliRunner()
        runner.invoke(main, ["init", "--project", str(tmp_path)])
        first = (tmp_path / ".mcp.json").read_text()

        runner.invoke(main, ["init", "--project", str(tmp_path)])
        second = (tmp_path / ".mcp.json").read_text()

        assert first == second
