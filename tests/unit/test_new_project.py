"""Tests for ``callmem new`` git/github/coding-norms flags and the
``callmem new-project`` subcommand.

These tests verify:
- ``--git`` initializes a git repo and writes .gitignore
- ``--github`` implies ``--git`` and invokes ``gh repo create``
- ``--coding-norms`` writes AGENTS.md and CLAUDE.md from the full templates
- ``callmem new-project`` defaults all spin-up flags to True
- ``--visibility public`` is passed through
- GitHub credentials are never read, stored, or passed — only ``gh`` is
  invoked as a subprocess with the repo name and visibility flag
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from callmem.cli import main


class TestNewGitFlag:
    def test_git_init_creates_repo_and_gitignore(self, tmp_path: Path) -> None:
        target = tmp_path / "myproj"
        runner = CliRunner()
        result = runner.invoke(main, [
            "new", str(target), "--no-service", "--port", "9800", "--git",
        ])
        assert result.exit_code == 0, result.output
        assert (target / ".git").is_dir()
        assert (target / ".gitignore").exists()
        gi = (target / ".gitignore").read_text()
        assert "vault.key" in gi
        assert "vault.salt" in gi
        assert ".callmem/*" in gi

    def test_git_creates_initial_commit(self, tmp_path: Path) -> None:
        target = tmp_path / "myproj"
        runner = CliRunner()
        result = runner.invoke(main, [
            "new", str(target), "--no-service", "--port", "9801", "--git",
        ])
        assert result.exit_code == 0, result.output
        log = subprocess.run(
            ["git", "log", "--oneline"], cwd=target, capture_output=True, text=True,
        )
        assert log.returncode == 0
        assert "initial commit" in log.stdout

    def test_no_git_flag_skips_git_init(self, tmp_path: Path) -> None:
        target = tmp_path / "nogit"
        runner = CliRunner()
        result = runner.invoke(main, [
            "new", str(target), "--no-service", "--port", "9802",
        ])
        assert result.exit_code == 0, result.output
        assert not (target / ".git").is_dir()
        assert (target / ".gitignore").exists()


class TestNewGithubFlag:
    def test_github_implies_git(self, tmp_path: Path) -> None:
        target = tmp_path / "ghproj"
        runner = CliRunner()
        with patch("callmem.cli._create_github_repo", return_value=True) as mock_gh:
            result = runner.invoke(main, [
                "new", str(target), "--no-service", "--port", "9803", "--github",
            ])
        assert result.exit_code == 0, result.output
        assert (target / ".git").is_dir()
        assert (target / ".gitignore").exists()
        mock_gh.assert_called_once()

    def test_github_invokes_gh_with_private_by_default(self, tmp_path: Path) -> None:
        target = tmp_path / "ghprivate"
        runner = CliRunner()
        with patch("callmem.cli._create_github_repo", return_value=True) as mock_gh:
            result = runner.invoke(main, [
                "new", str(target), "--no-service", "--port", "9804", "--github",
            ])
        assert result.exit_code == 0, result.output
        mock_gh.assert_called_once_with(target.resolve(), "ghprivate", "private")

    def test_github_visibility_public(self, tmp_path: Path) -> None:
        target = tmp_path / "ghpublic"
        runner = CliRunner()
        with patch("callmem.cli._create_github_repo", return_value=True) as mock_gh:
            result = runner.invoke(main, [
                "new", str(target), "--no-service", "--port", "9805",
                "--github", "--visibility", "public",
            ])
        assert result.exit_code == 0, result.output
        mock_gh.assert_called_once_with(target.resolve(), "ghpublic", "public")

    def test_github_failure_does_not_crash(self, tmp_path: Path) -> None:
        target = tmp_path / "ghfail"
        runner = CliRunner()
        with patch("callmem.cli.subprocess.run") as mock_run:
            def side_effect(args, **kwargs):
                if args[0:2] == ["gh", "repo"]:
                    return subprocess.CompletedProcess(args, 1, b"", b"error")
                return subprocess.CompletedProcess(args, 0, b"", b"")

            mock_run.side_effect = side_effect
            result = runner.invoke(main, [
                "new", str(target), "--no-service", "--port", "9806", "--github",
            ])
        assert result.exit_code == 0, result.output
        assert "failed" in result.output.lower()

    def test_no_credentials_passed_to_gh(self, tmp_path: Path) -> None:
        """Verify gh is only called with repo name + visibility — no tokens."""
        target = tmp_path / "creds"
        runner = CliRunner()
        with patch("callmem.cli.subprocess.run") as mock_run:
            def side_effect(args, **kwargs):
                if args[0:2] == ["gh", "repo"]:
                    return subprocess.CompletedProcess(args, 0, b"", b"")
                return subprocess.CompletedProcess(args, 0, b"", b"")

            mock_run.side_effect = side_effect
            runner.invoke(main, [
                "new", str(target), "--no-service", "--port", "9807", "--github",
            ])
        for call in mock_run.call_args_list:
            args = call.args[0]
            joined = " ".join(args)
            assert "token" not in joined.lower()
            assert "GITHUB_TOKEN" not in joined
            assert "GH_TOKEN" not in joined
            assert "password" not in joined.lower()
            env = call.kwargs.get("env")
            assert env is None or "GITHUB_TOKEN" not in env


class TestCodingNormsFlag:
    def test_writes_agents_md_with_coding_norms(self, tmp_path: Path) -> None:
        target = tmp_path / "norms"
        runner = CliRunner()
        result = runner.invoke(main, [
            "new", str(target), "--no-service", "--port", "9808", "--coding-norms",
        ])
        assert result.exit_code == 0, result.output
        agents = (target / "AGENTS.md").read_text()
        assert "Think before coding" in agents
        assert "Simplicity first" in agents
        assert "Surgical changes" in agents
        assert "No AI attribution" in agents
        assert "callmem" in agents
        assert "mem_ingest" in agents
        assert "## Project stack" in agents

    def test_writes_claude_md(self, tmp_path: Path) -> None:
        target = tmp_path / "norms2"
        runner = CliRunner()
        result = runner.invoke(main, [
            "new", str(target), "--no-service", "--port", "9809", "--coding-norms",
        ])
        assert result.exit_code == 0, result.output
        claude = (target / "CLAUDE.md").read_text()
        assert "AGENTS.md" in claude
        assert "callmem" in claude

    def test_project_name_substituted_in_agents(self, tmp_path: Path) -> None:
        target = tmp_path / "namedproj"
        runner = CliRunner()
        result = runner.invoke(main, [
            "new", str(target), "--no-service", "--port", "9810",
            "--coding-norms", "--name", "my-cool-project",
        ])
        assert result.exit_code == 0, result.output
        agents = (target / "AGENTS.md").read_text()
        assert "my-cool-project" in agents
        assert "{project_name}" not in agents

    def test_no_coding_norms_uses_basic_template(self, tmp_path: Path) -> None:
        target = tmp_path / "basic"
        runner = CliRunner()
        result = runner.invoke(main, [
            "new", str(target), "--no-service", "--port", "9811",
        ])
        assert result.exit_code == 0, result.output
        agents = (target / "AGENTS.md").read_text()
        assert "Project Memory" in agents
        assert "Think before coding" not in agents
        assert not (target / "CLAUDE.md").exists()


class TestNewProjectSubcommand:
    def test_defaults_all_flags_true(self, tmp_path: Path) -> None:
        target = tmp_path / "fullproj"
        runner = CliRunner()
        with patch("callmem.cli._create_github_repo", return_value=True) as mock_gh:
            result = runner.invoke(main, [
                "new-project", str(target), "--no-service", "--port", "9812",
            ])
        assert result.exit_code == 0, result.output
        assert (target / ".git").is_dir()
        assert (target / ".gitignore").exists()
        assert (target / "AGENTS.md").exists()
        assert (target / "CLAUDE.md").exists()
        agents = (target / "AGENTS.md").read_text()
        assert "Think before coding" in agents
        log = subprocess.run(
            ["git", "log", "--oneline"], cwd=target, capture_output=True, text=True,
        )
        assert "initial commit" in log.stdout
        mock_gh.assert_called_once_with(target.resolve(), "fullproj", "private")

    def test_no_github_skips_remote(self, tmp_path: Path) -> None:
        target = tmp_path / "gitonly"
        runner = CliRunner()
        with patch("callmem.cli._create_github_repo", return_value=True) as mock_gh:
            result = runner.invoke(main, [
                "new-project", str(target), "--no-service", "--port", "9813",
                "--no-github",
            ])
        assert result.exit_code == 0, result.output
        assert (target / ".git").is_dir()
        mock_gh.assert_not_called()

    def test_visibility_public_passes_through(self, tmp_path: Path) -> None:
        target = tmp_path / "pubproj"
        runner = CliRunner()
        with patch("callmem.cli._create_github_repo", return_value=True) as mock_gh:
            result = runner.invoke(main, [
                "new-project", str(target), "--no-service", "--port", "9814",
                "--visibility", "public",
            ])
        assert result.exit_code == 0, result.output
        mock_gh.assert_called_once_with(target.resolve(), "pubproj", "public")

    def test_inherits_from_donor(self, tmp_path: Path) -> None:
        donor = tmp_path / "donor"
        runner = CliRunner()
        runner.invoke(main, [
            "new", str(donor), "--no-service", "--port", "9815",
        ])
        donor_cfg = donor / ".callmem" / "config.toml"
        text = donor_cfg.read_text().replace(
            'model = "qwen3:8b"', 'model = "llama3:70b"',
        )
        donor_cfg.write_text(text)

        target = tmp_path / "child"
        with patch("callmem.cli._create_github_repo", return_value=True):
            result = runner.invoke(main, [
                "new-project", str(target), "--from", str(donor),
                "--no-service", "--port", "9816",
            ])
        assert result.exit_code == 0, result.output
        child_config = (target / ".callmem" / "config.toml").read_text()
        assert 'model = "llama3:70b"' in child_config
        assert 'name = "child"' in child_config

    def test_refuses_to_clobber_existing(self, tmp_path: Path) -> None:
        runner = CliRunner()
        runner.invoke(main, [
            "new-project", str(tmp_path / "p"), "--no-service", "--port", "9817",
        ])
        result = runner.invoke(main, [
            "new-project", str(tmp_path / "p"), "--no-service", "--port", "9818",
        ])
        assert result.exit_code == 1
        assert "already exists" in result.output


class TestGitignoreTemplate:
    def test_gitignore_covers_vault_and_db(self, tmp_path: Path) -> None:
        target = tmp_path / "gitest"
        runner = CliRunner()
        result = runner.invoke(main, [
            "new", str(target), "--no-service", "--port", "9819", "--git",
        ])
        assert result.exit_code == 0, result.output
        gi = (target / ".gitignore").read_text()
        assert ".callmem/*" in gi
        assert "vault.key" in gi
        assert "vault.salt" in gi
        assert "__pycache__" in gi

    def test_gitignore_appends_to_existing(self, tmp_path: Path) -> None:
        target = tmp_path / "existinggi"
        target.mkdir()
        (target / ".gitignore").write_text("# my project\n*.log\n")
        runner = CliRunner()
        result = runner.invoke(main, [
            "new", str(target), "--no-service", "--port", "9820", "--git",
        ])
        assert result.exit_code == 0, result.output
        gi = (target / ".gitignore").read_text()
        assert "*.log" in gi
        assert "vault.key" in gi
        assert "vault.salt" in gi
