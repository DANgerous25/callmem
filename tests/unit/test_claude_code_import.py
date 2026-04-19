"""Tests for the Claude Code transcript importer (WO-37 batch path)."""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner

from llm_mem.adapters.claude_code_import import (
    _map_record,
    claude_project_dir,
    discover_sessions,
    import_session,
    import_sessions,
    project_slug,
)
from llm_mem.cli import main
from llm_mem.core.config import load_config
from llm_mem.core.database import Database
from llm_mem.core.engine import MemoryEngine

if TYPE_CHECKING:
    from pathlib import Path


def _make_engine(project: Path) -> MemoryEngine:
    runner = CliRunner()
    result = runner.invoke(main, ["init", "--project", str(project)])
    assert result.exit_code == 0
    config = load_config(project)
    db = Database(project / ".llm-mem" / "memory.db")
    db.initialize()
    return MemoryEngine(db, config)


def _write_transcript(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")


class TestSlug:
    def test_slug_matches_claude_encoding(self, tmp_path: Path) -> None:
        sample = tmp_path / "subdir"
        sample.mkdir()
        slug = project_slug(sample)
        assert slug.startswith("-")
        assert "/" not in slug
        assert slug == str(sample.resolve()).replace("/", "-")


class TestMapRecord:
    def test_user_string_content_is_prompt(self) -> None:
        events = _map_record({
            "type": "user",
            "message": {"role": "user", "content": "hello"},
            "timestamp": "2026-04-19T10:00:00.000Z",
        })
        assert len(events) == 1
        assert events[0].type == "prompt"
        assert events[0].content == "hello"
        assert events[0].timestamp == "2026-04-19T10:00:00.000Z"

    def test_user_meta_is_skipped(self) -> None:
        assert _map_record({
            "type": "user",
            "isMeta": True,
            "message": {"role": "user", "content": "x"},
        }) == []

    def test_user_tool_result_list_skipped(self) -> None:
        assert _map_record({
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}],
            },
        }) == []

    def test_assistant_text_block_is_response(self) -> None:
        events = _map_record({
            "type": "assistant",
            "message": {"role": "assistant", "content": [
                {"type": "text", "text": "hi there"},
            ]},
        })
        assert [e.type for e in events] == ["response"]
        assert events[0].content == "hi there"

    def test_assistant_tool_use_block_is_tool_call(self) -> None:
        events = _map_record({
            "type": "assistant",
            "message": {"role": "assistant", "content": [
                {"type": "tool_use", "name": "Bash",
                 "input": {"command": "ls -la"}},
            ]},
        })
        assert len(events) == 1
        assert events[0].type == "tool_call"
        assert "Bash" in events[0].content
        assert "ls -la" in events[0].content

    def test_assistant_thinking_is_skipped(self) -> None:
        events = _map_record({
            "type": "assistant",
            "message": {"content": [{"type": "thinking", "thinking": "hm"}]},
        })
        assert events == []

    def test_mixed_text_and_tool_use_emits_both(self) -> None:
        events = _map_record({
            "type": "assistant",
            "message": {"content": [
                {"type": "text", "text": "running bash"},
                {"type": "tool_use", "name": "Bash", "input": {"cmd": "ls"}},
            ]},
        })
        assert [e.type for e in events] == ["response", "tool_call"]

    @pytest.mark.parametrize("rtype", [
        "permission-mode",
        "attachment",
        "file-history-snapshot",
        "last-prompt",
        "system/turn_duration",
        "system/away_summary",
        "system/informational",
    ])
    def test_irrelevant_types_are_skipped(self, rtype: str) -> None:
        assert _map_record({"type": rtype}) == []

    def test_slash_command_is_kept_as_prompt(self) -> None:
        events = _map_record({
            "type": "user",
            "message": {"content": "<command-name>/loop</command-name>"},
        })
        assert len(events) == 1
        assert events[0].type == "prompt"


class TestDiscover:
    def test_returns_nothing_when_cc_dir_absent(self, tmp_path: Path) -> None:
        projects_root = tmp_path / "claude-projects"
        projects_root.mkdir()
        assert discover_sessions(
            tmp_path / "my-project", claude_projects_dir=projects_root,
        ) == []

    def test_finds_transcripts(self, tmp_path: Path) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        projects_root = tmp_path / "claude-projects"
        cc_dir = claude_project_dir(project, projects_root)
        _write_transcript(
            cc_dir / "aaa.jsonl",
            [
                {"type": "user", "message": {"content": "first prompt"},
                 "timestamp": "2026-04-19T10:00:00Z"},
                {"type": "assistant", "message": {
                    "content": [{"type": "text", "text": "hi"}]},
                    "timestamp": "2026-04-19T10:00:01Z"},
            ],
        )
        sessions = discover_sessions(project, claude_projects_dir=projects_root)
        assert len(sessions) == 1
        assert sessions[0]["id"] == "aaa"
        assert sessions[0]["title"].startswith("first prompt")
        assert sessions[0]["message_count"] == 2


class TestImportSession:
    def test_ingests_events_and_ends_session(self, tmp_path: Path) -> None:
        engine = _make_engine(tmp_path)
        jsonl = tmp_path / "cc.jsonl"
        _write_transcript(jsonl, [
            {"type": "user", "message": {"content": "build the thing"},
             "timestamp": "2026-04-19T10:00:00Z"},
            {"type": "assistant", "message": {
                "model": "claude-opus-4-7",
                "content": [{"type": "text", "text": "on it"}]},
             "timestamp": "2026-04-19T10:00:01Z"},
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Bash",
                 "input": {"command": "make"}}]},
             "timestamp": "2026-04-19T10:00:02Z"},
        ])

        result = import_session(engine, jsonl)
        assert result["event_count"] == 3
        assert not result.get("skipped")

        db_path = tmp_path / ".llm-mem" / "memory.db"
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            sess = conn.execute(
                "SELECT agent_name, model_name, status, summary, metadata "
                "FROM sessions WHERE id=?",
                (result["session_id"],),
            ).fetchone()
            assert sess is not None
            assert sess[0] == "claude-code"
            assert sess[1] == "claude-opus-4-7"
            assert sess[2] == "ended"
            assert sess[3].startswith("build the thing")
            meta = json.loads(sess[4])
            assert meta["source_type"] == "claude-code"
            assert meta["source_id"] == "cc"

            types = [row[0] for row in conn.execute(
                "SELECT type FROM events WHERE session_id=? "
                "ORDER BY timestamp",
                (result["session_id"],),
            ).fetchall()]
            assert types == ["prompt", "response", "tool_call"]
        finally:
            conn.close()

    def test_idempotent_reimport(self, tmp_path: Path) -> None:
        engine = _make_engine(tmp_path)
        jsonl = tmp_path / "cc.jsonl"
        _write_transcript(jsonl, [
            {"type": "user", "message": {"content": "hello"},
             "timestamp": "2026-04-19T10:00:00Z"},
        ])

        first = import_session(engine, jsonl)
        assert not first.get("skipped")

        second = import_session(engine, jsonl)
        assert second.get("skipped") is True
        assert second["session_id"] == first["session_id"]

        db_path = tmp_path / ".llm-mem" / "memory.db"
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            n = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
            assert n == 1
        finally:
            conn.close()


class TestImportSessions:
    def test_dry_run_lists_without_ingesting(self, tmp_path: Path) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        projects_root = tmp_path / "claude-projects"
        cc_dir = claude_project_dir(project, projects_root)
        _write_transcript(cc_dir / "aaa.jsonl", [
            {"type": "user", "message": {"content": "a"},
             "timestamp": "2026-04-19T10:00:00Z"},
        ])
        engine = _make_engine(project)

        # monkey-patch import_session so dry-run never ingests (it shouldn't,
        # but we confirm via the sessions table)
        results = import_sessions(
            engine,
            project_path=project,
            claude_projects_dir=projects_root,
            dry_run=True,
        )
        assert results and results[0].get("dry_run") is True

        db_path = project / ".llm-mem" / "memory.db"
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            n = conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE agent_name='claude-code'"
            ).fetchone()[0]
            assert n == 0
        finally:
            conn.close()

    def test_real_import_ingests_all(self, tmp_path: Path) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        projects_root = tmp_path / "claude-projects"
        cc_dir = claude_project_dir(project, projects_root)
        _write_transcript(cc_dir / "aaa.jsonl", [
            {"type": "user", "message": {"content": "first"},
             "timestamp": "2026-04-19T10:00:00Z"},
            {"type": "assistant", "message": {
                "content": [{"type": "text", "text": "ok"}]},
             "timestamp": "2026-04-19T10:00:01Z"},
        ])
        _write_transcript(cc_dir / "bbb.jsonl", [
            {"type": "user", "message": {"content": "second"},
             "timestamp": "2026-04-19T11:00:00Z"},
        ])
        engine = _make_engine(project)

        results = import_sessions(
            engine, project_path=project,
            claude_projects_dir=projects_root,
        )
        assert len(results) == 2
        assert all(not r.get("skipped") for r in results)


class TestImportCLI:
    def test_cli_claude_code_source(self, tmp_path: Path) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        projects_root = tmp_path / "claude-projects"
        cc_dir = claude_project_dir(project, projects_root)
        _write_transcript(cc_dir / "aaa.jsonl", [
            {"type": "user", "message": {"content": "hi"},
             "timestamp": "2026-04-19T10:00:00Z"},
        ])

        runner = CliRunner()
        runner.invoke(main, ["init", "--project", str(project)])

        import llm_mem.adapters.claude_code_import as cci
        original = cci.CLAUDE_PROJECTS_DIR
        cci.CLAUDE_PROJECTS_DIR = projects_root
        try:
            result = runner.invoke(main, [
                "import", "--source", "claude-code",
                "--project", str(project),
            ])
        finally:
            cci.CLAUDE_PROJECTS_DIR = original

        assert result.exit_code == 0, result.output
        assert "Discovered 1 sessions" in result.output
        db_path = project / ".llm-mem" / "memory.db"
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            n = conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE agent_name='claude-code'"
            ).fetchone()[0]
            assert n == 1
        finally:
            conn.close()
