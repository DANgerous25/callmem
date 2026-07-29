"""Tests for the live Claude Code tailer."""

from __future__ import annotations

import json
import sqlite3
import time
from typing import TYPE_CHECKING

from click.testing import CliRunner

from callmem.adapters.claude_code import ClaudeCodeAdapter
from callmem.adapters.claude_code_import import (
    claude_project_dir,
)
from callmem.cli import main
from callmem.core.config import load_config
from callmem.core.database import Database
from callmem.core.engine import MemoryEngine

if TYPE_CHECKING:
    from pathlib import Path


def _make_engine(project: Path) -> MemoryEngine:
    runner = CliRunner()
    result = runner.invoke(main, ["init", "--project", str(project)])
    assert result.exit_code == 0
    config = load_config(project)
    db = Database(project / ".callmem" / "memory.db")
    db.initialize()
    return MemoryEngine(db, config)


def _make_engine_with_skip_tools(project: Path, skip_tools: list[str]) -> MemoryEngine:
    from callmem.models.config import Config

    runner = CliRunner()
    result = runner.invoke(main, ["init", "--project", str(project)])
    assert result.exit_code == 0
    config = Config(ingestion={"skip_tools": skip_tools})
    db = Database(project / ".callmem" / "memory.db")
    db.initialize()
    return MemoryEngine(db, config)


def _append_records(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")


def _count_events(project: Path, agent_name: str = "claude-code") -> int:
    db_path = project / ".callmem" / "memory.db"
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM events e "
            "JOIN sessions s ON s.id = e.session_id "
            "WHERE s.agent_name = ?",
            (agent_name,),
        ).fetchone()[0]
    finally:
        conn.close()


def _session_status(project: Path, source_id: str) -> str | None:
    db_path = project / ".callmem" / "memory.db"
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT status, metadata FROM sessions ORDER BY started_at DESC"
        ).fetchall()
        for status, meta in rows:
            if not meta:
                continue
            parsed = json.loads(meta)
            if parsed.get("source_id") == source_id:
                return str(status)
    finally:
        conn.close()
    return None


class TestOffsetTracking:
    def test_first_tick_ingests_all_existing_lines(self, tmp_path: Path) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        roots = tmp_path / "claude-projects"
        cc_dir = claude_project_dir(project, roots)
        transcript = cc_dir / "aaa.jsonl"
        _append_records(transcript, [
            {"type": "user", "message": {"content": "hi"},
             "timestamp": "2026-04-19T10:00:00Z"},
            {"type": "assistant", "message": {
                "content": [{"type": "text", "text": "hello"}]},
             "timestamp": "2026-04-19T10:00:01Z"},
        ])
        engine = _make_engine(project)
        adapter = ClaudeCodeAdapter(
            engine, project_path=project, claude_projects_dir=roots,
        )
        adapter._tick()

        assert _count_events(project) == 2
        # Offset advanced past the last newline.
        assert adapter._offsets[str(transcript)] == transcript.stat().st_size

    def test_second_tick_only_reads_new_lines(self, tmp_path: Path) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        roots = tmp_path / "claude-projects"
        transcript = claude_project_dir(project, roots) / "aaa.jsonl"
        _append_records(transcript, [
            {"type": "user", "message": {"content": "first"},
             "timestamp": "2026-04-19T10:00:00Z"},
        ])
        engine = _make_engine(project)
        adapter = ClaudeCodeAdapter(
            engine, project_path=project, claude_projects_dir=roots,
        )
        adapter._tick()
        assert _count_events(project) == 1

        _append_records(transcript, [
            {"type": "assistant", "message": {
                "content": [{"type": "text", "text": "second"}]},
             "timestamp": "2026-04-19T10:00:01Z"},
        ])
        adapter._tick()
        assert _count_events(project) == 2

    def test_partial_final_line_deferred(self, tmp_path: Path) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        roots = tmp_path / "claude-projects"
        transcript = claude_project_dir(project, roots) / "aaa.jsonl"
        transcript.parent.mkdir(parents=True, exist_ok=True)

        first = json.dumps({
            "type": "user", "message": {"content": "done"},
            "timestamp": "2026-04-19T10:00:00Z",
        })
        # Second line is complete JSON but lacks a trailing newline —
        # the writer hasn't flushed the terminator yet.
        second = json.dumps({
            "type": "user", "message": {"content": "pending"},
            "timestamp": "2026-04-19T10:00:01Z",
        })
        transcript.write_text(first + "\n" + second)

        engine = _make_engine(project)
        adapter = ClaudeCodeAdapter(
            engine, project_path=project, claude_projects_dir=roots,
        )
        adapter._tick()
        assert _count_events(project) == 1
        # Offset stops after the first newline — the pending line has
        # no terminator yet, so we haven't consumed it.
        assert adapter._offsets[str(transcript)] == len(first) + 1

        # The writer finishes the second line.
        with transcript.open("a", encoding="utf-8") as fh:
            fh.write("\n")
        adapter._tick()
        assert _count_events(project) == 2

    def test_offsets_persisted_across_instances(self, tmp_path: Path) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        roots = tmp_path / "claude-projects"
        transcript = claude_project_dir(project, roots) / "aaa.jsonl"
        _append_records(transcript, [
            {"type": "user", "message": {"content": "one"},
             "timestamp": "2026-04-19T10:00:00Z"},
        ])
        engine = _make_engine(project)

        a1 = ClaudeCodeAdapter(
            engine, project_path=project, claude_projects_dir=roots,
        )
        a1._tick()
        a1._save_offsets()

        # Fresh adapter over the same project.
        a2 = ClaudeCodeAdapter(
            engine, project_path=project, claude_projects_dir=roots,
        )
        assert a2._offsets.get(str(transcript)) == transcript.stat().st_size

        # Append and tick the new adapter — only new line ingested.
        _append_records(transcript, [
            {"type": "assistant", "message": {
                "content": [{"type": "text", "text": "two"}]},
             "timestamp": "2026-04-19T10:00:01Z"},
        ])
        a2._tick()
        assert _count_events(project) == 2


class TestToolResultIngestion:
    def test_tool_result_ingested_with_tool_name_from_prior_tool_use(
        self, tmp_path: Path,
    ) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        roots = tmp_path / "claude-projects"
        transcript = claude_project_dir(project, roots) / "aaa.jsonl"
        _append_records(transcript, [
            {"type": "user", "message": {"content": "run ls"},
             "timestamp": "2026-04-19T10:00:00Z"},
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "toolu_1", "name": "Bash",
                 "input": {"command": "ls"}}]},
             "timestamp": "2026-04-19T10:00:01Z"},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "toolu_1",
                 "content": "file1.txt"}]},
             "timestamp": "2026-04-19T10:00:02Z"},
        ])
        engine = _make_engine(project)
        adapter = ClaudeCodeAdapter(
            engine, project_path=project, claude_projects_dir=roots,
        )
        adapter._tick()

        db_path = project / ".callmem" / "memory.db"
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT type, content, metadata FROM events ORDER BY timestamp"
            ).fetchall()
        finally:
            conn.close()

        types = [r[0] for r in rows]
        assert types == ["prompt", "tool_call", "tool_result"]
        meta = json.loads(rows[2][2])
        assert meta["tool_use_id"] == "toolu_1"
        assert meta["tool_name"] == "Bash"


class TestToolNamePersistenceAcrossRestart:
    def test_tool_name_survives_restart_so_skip_tools_still_applies(
        self, tmp_path: Path,
    ) -> None:
        """A crash/redeploy between a tool_use and its tool_result must
        not lose the tool_use_id -> tool_name resolution — otherwise
        skip_tools silently fails open for that result (see review
        finding: offsets are persisted every tick but the in-memory-only
        tool name cache previously was not).
        """
        project = tmp_path / "proj"
        project.mkdir()
        roots = tmp_path / "claude-projects"
        transcript = claude_project_dir(project, roots) / "aaa.jsonl"
        _append_records(transcript, [
            {"type": "user", "message": {"content": "run a skipped tool"},
             "timestamp": "2026-04-19T10:00:00Z"},
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "toolu_skip", "name": "Bash",
                 "input": {"command": "rm -rf /tmp/x"}}]},
             "timestamp": "2026-04-19T10:00:01Z"},
        ])
        engine = _make_engine_with_skip_tools(project, skip_tools=["Bash"])

        adapter1 = ClaudeCodeAdapter(
            engine, project_path=project, claude_projects_dir=roots,
        )
        adapter1._tick()
        assert adapter1._tool_names.get("aaa", {}).get("toolu_skip") == "Bash"
        # Simulate the process dying here — no explicit shutdown/save
        # beyond what _tick() already persisted opportunistically.
        del adapter1

        # Fresh process, same state dir.
        adapter2 = ClaudeCodeAdapter(
            engine, project_path=project, claude_projects_dir=roots,
        )
        assert adapter2._tool_names.get("aaa", {}).get("toolu_skip") == "Bash"

        _append_records(transcript, [
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "toolu_skip",
                 "content": "rm: permission denied"}]},
             "timestamp": "2026-04-19T10:00:02Z"},
        ])
        adapter2._tick()

        db_path = project / ".callmem" / "memory.db"
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            types = [r[0] for r in conn.execute(
                "SELECT type FROM events ORDER BY timestamp"
            ).fetchall()]
        finally:
            conn.close()

        # Both the tool_call and its result are skipped — the result's
        # tool name resolved via the *persisted* map, not an empty cache.
        assert types == ["prompt"]
        assert "toolu_skip" not in adapter2._tool_names.get("aaa", {})

        # And the pruned state is itself persisted, so a third restart
        # doesn't resurrect a stale entry.
        adapter3 = ClaudeCodeAdapter(
            engine, project_path=project, claude_projects_dir=roots,
        )
        assert "toolu_skip" not in adapter3._tool_names.get("aaa", {})


class TestSessionLifecycle:
    def test_session_opens_on_first_record(self, tmp_path: Path) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        roots = tmp_path / "claude-projects"
        transcript = claude_project_dir(project, roots) / "xxx.jsonl"
        _append_records(transcript, [
            {"type": "user", "message": {"content": "hi"},
             "timestamp": "2026-04-19T10:00:00Z"},
        ])
        engine = _make_engine(project)
        adapter = ClaudeCodeAdapter(
            engine, project_path=project, claude_projects_dir=roots,
        )
        adapter._tick()

        assert _session_status(project, "xxx") == "active"

    def test_idle_timeout_closes_session(self, tmp_path: Path) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        roots = tmp_path / "claude-projects"
        transcript = claude_project_dir(project, roots) / "yyy.jsonl"
        _append_records(transcript, [
            {"type": "user", "message": {"content": "hi"},
             "timestamp": "2026-04-19T10:00:00Z"},
        ])
        engine = _make_engine(project)
        adapter = ClaudeCodeAdapter(
            engine, project_path=project, claude_projects_dir=roots,
            idle_timeout=0.01,
        )
        adapter._tick()
        assert _session_status(project, "yyy") == "active"

        time.sleep(0.02)
        adapter._tick()
        assert _session_status(project, "yyy") == "ended"

    def test_stop_closes_open_sessions(self, tmp_path: Path) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        roots = tmp_path / "claude-projects"
        transcript = claude_project_dir(project, roots) / "zzz.jsonl"
        _append_records(transcript, [
            {"type": "user", "message": {"content": "hi"},
             "timestamp": "2026-04-19T10:00:00Z"},
        ])
        engine = _make_engine(project)
        adapter = ClaudeCodeAdapter(
            engine, project_path=project, claude_projects_dir=roots,
            poll_interval=0.01,
        )

        import threading
        t = threading.Thread(target=adapter.run, daemon=True)
        t.start()
        # Give it a moment to pick up the record.
        time.sleep(0.1)
        adapter.stop()
        t.join(timeout=2)

        assert _session_status(project, "zzz") == "ended"

    def test_shrink_resets_offset(self, tmp_path: Path) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        roots = tmp_path / "claude-projects"
        transcript = claude_project_dir(project, roots) / "trunc.jsonl"
        _append_records(transcript, [
            {"type": "user", "message": {"content": "original-one"},
             "timestamp": "2026-04-19T10:00:00Z"},
            {"type": "user", "message": {"content": "original-two"},
             "timestamp": "2026-04-19T10:00:01Z"},
        ])
        engine = _make_engine(project)
        adapter = ClaudeCodeAdapter(
            engine, project_path=project, claude_projects_dir=roots,
        )
        adapter._tick()
        first_count = _count_events(project)
        assert first_count == 2

        # File shrinks below previous offset — rotate/restart case.
        transcript.write_text(
            json.dumps({
                "type": "user",
                "message": {"content": "fresh"},
                "timestamp": "2026-04-19T11:00:00Z",
            }) + "\n"
        )
        adapter._tick()
        assert _count_events(project) == first_count + 1
