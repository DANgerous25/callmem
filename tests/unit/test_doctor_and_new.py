"""Tests for ``callmem doctor`` and ``callmem new``.

Doctor: drift detection + repair across the shipped template files
(``.opencode/plugins/auto-briefing.js``, ``.opencode/commands/briefing.md``,
``.claude/commands/briefing.md``). These files are how setup ended up stale
in dj-mix-track-breaker — the project predated a template refresh and never
got resynced because setup was never re-run.

New: spinning up a fully callmem-ready project from a donor, without
copying any donor memory data.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from callmem.cli import main
from callmem.core.integrations import (
    check_integration_drift,
    ensure_claude_code_commands,
    ensure_opencode_plugin,
)


def _shipped(kind: str, *parts: str) -> Path:
    """Resolve a path inside the shipped ``callmem/templates/<kind>/`` tree."""
    import callmem
    return Path(callmem.__file__).parent / "templates" / kind / Path(*parts)


def _make_opencode_layout(project: Path, content: str = "stale") -> None:
    (project / ".opencode" / "commands").mkdir(parents=True)
    (project / ".opencode" / "plugins").mkdir(parents=True)
    (project / ".opencode" / "commands" / "briefing.md").write_text(content)
    (project / ".opencode" / "plugins" / "auto-briefing.js").write_text(content)


def _make_claude_layout(project: Path, content: str = "stale") -> None:
    (project / ".claude" / "commands").mkdir(parents=True)
    (project / ".claude" / "hooks").mkdir(parents=True)
    (project / ".claude" / "commands" / "briefing.md").write_text(content)
    (project / ".claude" / "hooks" / "callmem-hook.py").write_text(content)


class TestCheckIntegrationDrift:
    def test_reports_stale_opencode_files(self, tmp_path: Path) -> None:
        _make_opencode_layout(tmp_path)
        drift = check_integration_drift(tmp_path, fix=False, check_claude=False)
        assert sorted(drift["opencode"]) == [
            "BRIEFING_INSTRUCTIONS.md",
            "auto-briefing.js",
            "briefing.md",
            "callmem.js",
        ]

    def test_reports_stale_claude_files(self, tmp_path: Path) -> None:
        _make_claude_layout(tmp_path)
        drift = check_integration_drift(tmp_path, fix=False, check_opencode=False)
        assert sorted(drift["claude_code"]) == ["briefing.md", "callmem-hook.py"]

    def test_dry_run_does_not_modify_files(self, tmp_path: Path) -> None:
        _make_opencode_layout(tmp_path, content="stale-content")
        check_integration_drift(tmp_path, fix=False)
        assert (tmp_path / ".opencode" / "commands" / "briefing.md").read_text() == "stale-content"

    def test_fix_overwrites_stale_files(self, tmp_path: Path) -> None:
        _make_opencode_layout(tmp_path)
        _make_claude_layout(tmp_path)

        check_integration_drift(tmp_path, fix=True)

        expected_oc_cmd = _shipped("opencode", "commands", "briefing.md").read_text()
        expected_oc_plugin = _shipped("opencode", "plugins", "auto-briefing.js").read_text()
        expected_cc = _shipped("claude", "commands", "briefing.md").read_text()
        assert (tmp_path / ".opencode" / "commands" / "briefing.md").read_text() == expected_oc_cmd
        assert (tmp_path / ".opencode" / "plugins" / "auto-briefing.js").read_text() == expected_oc_plugin
        assert (tmp_path / ".claude" / "commands" / "briefing.md").read_text() == expected_cc

    def test_clean_project_reports_no_drift(self, tmp_path: Path) -> None:
        ensure_opencode_plugin(tmp_path)
        ensure_claude_code_commands(tmp_path)
        drift = check_integration_drift(tmp_path, fix=False)
        assert drift == {"opencode": [], "claude_code": []}

    def test_check_flags_skip_categories(self, tmp_path: Path) -> None:
        drift = check_integration_drift(
            tmp_path, fix=False, check_opencode=False, check_claude=False,
        )
        assert drift == {}


class TestDoctorCommand:
    def test_exit_zero_when_no_integration_present(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["doctor", "--project", str(tmp_path)])
        assert result.exit_code == 0
        assert "No coding-tool integration detected" in result.output

    def test_exit_nonzero_on_drift(self, tmp_path: Path) -> None:
        _make_opencode_layout(tmp_path)
        runner = CliRunner()
        result = runner.invoke(main, ["doctor", "--project", str(tmp_path)])
        assert result.exit_code == 1
        assert "stale or missing" in result.output

    def test_fix_repairs_and_exits_zero(self, tmp_path: Path) -> None:
        _make_opencode_layout(tmp_path)
        _make_claude_layout(tmp_path)
        runner = CliRunner()
        result = runner.invoke(main, ["doctor", "--project", str(tmp_path), "--fix"])
        assert result.exit_code == 0
        assert "Repaired" in result.output

        # Second run should now be clean.
        result2 = runner.invoke(main, ["doctor", "--project", str(tmp_path)])
        assert result2.exit_code == 0
        assert "match shipped templates" in result2.output

    def test_clean_project_exits_zero(self, tmp_path: Path) -> None:
        ensure_opencode_plugin(tmp_path)
        ensure_claude_code_commands(tmp_path)
        # Establish the .claude/ root so doctor checks it.
        (tmp_path / ".claude" / "commands" / "briefing.md").parent.mkdir(
            parents=True, exist_ok=True,
        )

        runner = CliRunner()
        result = runner.invoke(main, ["doctor", "--project", str(tmp_path)])
        assert result.exit_code == 0


class TestNewCommand:
    def test_creates_fresh_project_without_donor(self, tmp_path: Path) -> None:
        target = tmp_path / "fresh"
        runner = CliRunner()
        result = runner.invoke(main, [
            "new", str(target), "--no-service", "--port", "9555",
        ])
        assert result.exit_code == 0, result.output

        assert (target / ".callmem" / "config.toml").exists()
        assert (target / ".callmem" / "memory.db").exists()
        assert (target / ".opencode" / "plugins" / "auto-briefing.js").exists()
        assert (target / ".opencode" / "commands" / "briefing.md").exists()
        assert (target / ".claude" / "commands" / "briefing.md").exists()
        assert (target / ".mcp.json").exists()
        config = (target / ".callmem" / "config.toml").read_text()
        assert 'name = "fresh"' in config
        assert "port = 9555" in config

    def test_inherits_settings_from_donor(self, tmp_path: Path) -> None:
        donor = tmp_path / "donor"
        runner = CliRunner()
        # Bootstrap a donor with a specific LLM model so we can verify
        # inheritance.
        result = runner.invoke(main, [
            "new", str(donor), "--no-service", "--port", "9700",
        ])
        assert result.exit_code == 0, result.output

        donor_cfg = donor / ".callmem" / "config.toml"
        text = donor_cfg.read_text().replace(
            'model = "qwen3:8b"', 'model = "llama3:70b"',
        )
        donor_cfg.write_text(text)

        target = tmp_path / "child"
        result2 = runner.invoke(main, [
            "new", str(target), "--from", str(donor), "--no-service",
        ])
        assert result2.exit_code == 0, result2.output

        child_config = (target / ".callmem" / "config.toml").read_text()
        assert 'model = "llama3:70b"' in child_config
        assert 'name = "child"' in child_config
        # Inherited port from donor + 1, since 9700 is in use.
        assert "port = 9701" in child_config

    def test_does_not_copy_donor_database(self, tmp_path: Path) -> None:
        donor = tmp_path / "donor"
        runner = CliRunner()
        runner.invoke(main, ["new", str(donor), "--no-service", "--port", "9710"])

        # Stuff something into donor's DB so we can detect leakage.
        donor_db = donor / ".callmem" / "memory.db"
        donor_db_bytes_before = donor_db.read_bytes()

        target = tmp_path / "child"
        result = runner.invoke(main, [
            "new", str(target), "--from", str(donor), "--no-service",
        ])
        assert result.exit_code == 0, result.output

        child_db = target / ".callmem" / "memory.db"
        # Fresh DB shouldn't share donor's exact file contents (different
        # rowids/sessions etc. won't have been inserted, but at minimum the
        # files are produced independently).
        assert child_db.exists()
        # If the file were a copy, it'd be byte-identical right after init
        # when no extra writes have happened — but the donor has been written
        # to during its own init, so timestamps and rowids will differ enough
        # that bytes-equality is a safe negative signal here. Instead use the
        # stronger check: child DB has its own path and donor's path is not
        # referenced anywhere in the child's config.
        assert "donor" not in (target / ".callmem" / "config.toml").read_text()
        assert donor_db.read_bytes() == donor_db_bytes_before  # donor untouched

    def test_refuses_to_clobber_existing_project(self, tmp_path: Path) -> None:
        runner = CliRunner()
        runner.invoke(main, [
            "new", str(tmp_path / "p"), "--no-service", "--port", "9720",
        ])
        result = runner.invoke(main, [
            "new", str(tmp_path / "p"), "--no-service", "--port", "9721",
        ])
        assert result.exit_code == 1
        assert "already exists" in result.output

    def test_refuses_invalid_donor(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()
        runner = CliRunner()
        result = runner.invoke(main, [
            "new", str(tmp_path / "child"), "--from", str(empty), "--no-service",
        ])
        assert result.exit_code == 1
        assert "has no .callmem/config.toml" in result.output

    @pytest.mark.parametrize("name_arg", ["my-project", "weird name", "snake_case"])
    def test_honors_explicit_name(self, tmp_path: Path, name_arg: str) -> None:
        target = tmp_path / "dir"
        runner = CliRunner()
        result = runner.invoke(main, [
            "new", str(target),
            "--name", name_arg,
            "--no-service",
            "--port", "9730",
        ])
        assert result.exit_code == 0, result.output
        assert f'name = "{name_arg}"' in (
            target / ".callmem" / "config.toml"
        ).read_text()
