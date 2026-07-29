"""Tests for the A/B benchmark harness (scripts/ab_benchmark.py).

scripts/ is not an installed package, so the module under test is loaded
directly from its file path. All `claude` invocations are mocked -- these
tests never spawn a real Claude Code session (see docs/ab-benchmark.md
and the task-8 brief: "no real sessions in tests").
"""

from __future__ import annotations

import csv
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "ab_benchmark.py"


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("ab_benchmark", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ab_benchmark = _load_module()


# A canned example of the documented `claude -p ... --output-format json`
# result shape (type/subtype/usage/session_id/cost fields at top level).
CANNED_CLAUDE_JSON = json.dumps(
    {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "duration_ms": 4213,
        "duration_api_ms": 3980,
        "num_turns": 3,
        "result": "The retry logic lives in queue.py's fail() method.",
        "session_id": "abc123-session",
        "total_cost_usd": 0.0421,
        "usage": {
            "input_tokens": 1200,
            "cache_creation_input_tokens": 800,
            "cache_read_input_tokens": 9000,
            "output_tokens": 450,
        },
    }
)


class TestLoadTasks:
    def test_valid_task_file(self, tmp_path: Path) -> None:
        path = tmp_path / "tasks.json"
        path.write_text(
            json.dumps(
                [
                    {"id": "task-one", "prompt": "Explain X."},
                    {"id": "task-two", "prompt": "Explain Y."},
                ]
            )
        )
        tasks = ab_benchmark.load_tasks(path)
        assert [t.id for t in tasks] == ["task-one", "task-two"]
        assert tasks[0].prompt == "Explain X."

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ab_benchmark.TaskFileError):
            ab_benchmark.load_tasks(tmp_path / "nope.json")

    def test_invalid_json_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "tasks.json"
        path.write_text("{not json")
        with pytest.raises(ab_benchmark.TaskFileError):
            ab_benchmark.load_tasks(path)

    def test_non_list_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "tasks.json"
        path.write_text(json.dumps({"id": "x", "prompt": "y"}))
        with pytest.raises(ab_benchmark.TaskFileError):
            ab_benchmark.load_tasks(path)

    def test_empty_list_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "tasks.json"
        path.write_text("[]")
        with pytest.raises(ab_benchmark.TaskFileError):
            ab_benchmark.load_tasks(path)

    def test_missing_id_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "tasks.json"
        path.write_text(json.dumps([{"prompt": "Explain X."}]))
        with pytest.raises(ab_benchmark.TaskFileError):
            ab_benchmark.load_tasks(path)

    def test_missing_prompt_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "tasks.json"
        path.write_text(json.dumps([{"id": "task-one"}]))
        with pytest.raises(ab_benchmark.TaskFileError):
            ab_benchmark.load_tasks(path)

    def test_non_string_id_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "tasks.json"
        path.write_text(json.dumps([{"id": 1, "prompt": "Explain X."}]))
        with pytest.raises(ab_benchmark.TaskFileError):
            ab_benchmark.load_tasks(path)

    def test_default_llm_mem_task_file_is_valid(self) -> None:
        default_path = REPO_ROOT / "scripts" / "ab_tasks_llm_mem.json"
        tasks = ab_benchmark.load_tasks(default_path)
        assert len(tasks) == 3
        for task in tasks:
            assert task.id
            assert task.prompt
            # Analysis-only: must not instruct the agent to make edits.
            assert "do not implement" in task.prompt.lower()


class TestParseClaudeJson:
    def test_valid_payload(self) -> None:
        result = ab_benchmark.parse_claude_json(CANNED_CLAUDE_JSON)
        assert result.input_tokens == 1200
        assert result.output_tokens == 450
        # total includes cache tokens: 1200 + 450 + 800 + 9000
        assert result.total_tokens == 11450
        assert result.session_id == "abc123-session"

    def test_not_json_raises_with_raw_payload(self) -> None:
        with pytest.raises(ab_benchmark.ClaudeOutputError) as exc_info:
            ab_benchmark.parse_claude_json("not json at all")
        assert "not json at all" in str(exc_info.value)

    def test_missing_usage_raises_with_raw_payload(self) -> None:
        raw = json.dumps({"type": "result", "session_id": "x"})
        with pytest.raises(ab_benchmark.ClaudeOutputError) as exc_info:
            ab_benchmark.parse_claude_json(raw)
        assert "usage" in str(exc_info.value)
        assert raw in str(exc_info.value)

    def test_usage_missing_token_fields_raises(self) -> None:
        raw = json.dumps({"usage": {"cache_read_input_tokens": 5}})
        with pytest.raises(ab_benchmark.ClaudeOutputError):
            ab_benchmark.parse_claude_json(raw)

    def test_non_object_payload_raises(self) -> None:
        with pytest.raises(ab_benchmark.ClaudeOutputError):
            ab_benchmark.parse_claude_json("[1, 2, 3]")


class TestBuildClaudeCommand:
    def test_run_a_has_no_disable_flags(self) -> None:
        cmd = ab_benchmark.build_claude_command("do the thing", disable_mcp=False)
        assert cmd[:2] == ["claude", "-p"]
        assert "do the thing" in cmd
        assert "--output-format" in cmd and "json" in cmd
        assert "--strict-mcp-config" not in cmd

    def test_run_b_disables_mcp(self) -> None:
        cmd = ab_benchmark.build_claude_command("do the thing", disable_mcp=True)
        assert "--strict-mcp-config" in cmd
        assert "--mcp-config" in cmd
        idx = cmd.index("--mcp-config")
        assert cmd[idx + 1] == '{"mcpServers":{}}'


class TestRunClaude:
    def test_invokes_subprocess_and_parses_result(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, object] = {}

        def fake_run(
            cmd: list[str], *, cwd: Path, capture_output: bool, text: bool, check: bool
        ) -> subprocess.CompletedProcess[str]:
            captured["cmd"] = cmd
            captured["cwd"] = cwd
            return subprocess.CompletedProcess(cmd, 0, stdout=CANNED_CLAUDE_JSON, stderr="")

        monkeypatch.setattr(ab_benchmark.subprocess, "run", fake_run)
        result = ab_benchmark.run_claude("hello", tmp_path, disable_mcp=False)

        assert result.total_tokens == 11450
        assert captured["cwd"] == tmp_path
        assert "hello" in captured["cmd"]  # type: ignore[operator]

    def test_nonzero_exit_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_run(
            cmd: list[str], *, cwd: Path, capture_output: bool, text: bool, check: bool
        ) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom")

        monkeypatch.setattr(ab_benchmark.subprocess, "run", fake_run)
        with pytest.raises(ab_benchmark.ClaudeOutputError) as exc_info:
            ab_benchmark.run_claude("hello", tmp_path, disable_mcp=False)
        assert "boom" in str(exc_info.value)


class TestComputeDeltaPct:
    def test_matches_doc_example(self) -> None:
        # docs/ab-benchmark.md example row: 14820 / 18430 -> 19.6
        delta = ab_benchmark.compute_delta_pct(14820, 18430)
        assert f"{delta:.1f}" == "19.6"

    def test_zero_denominator_is_zero(self) -> None:
        assert ab_benchmark.compute_delta_pct(100, 0) == 0.0


class TestAppendCsvRow:
    def test_matches_doc_example_row(self, tmp_path: Path) -> None:
        csv_path = tmp_path / "ab-benchmark-results.csv"
        row = ab_benchmark.BenchmarkRow(
            date="2026-05-14",
            project="llm-mem",
            task="add --since flag to usage",
            run_a_tokens=14820,
            run_b_tokens=18430,
            delta_pct=ab_benchmark.compute_delta_pct(14820, 18430),
            run_a_reads=3,
            run_b_reads=7,
            citations="5",
        )
        ab_benchmark.append_csv_row(csv_path, row)

        with csv_path.open(newline="") as f:
            reader = csv.reader(f)
            rows = list(reader)

        assert rows[0] == ab_benchmark.CSV_HEADER
        assert rows[1] == [
            "2026-05-14",
            "llm-mem",
            "add --since flag to usage",
            "14820",
            "18430",
            "19.6",
            "3",
            "7",
            "5",
        ]

    def test_header_written_once_on_repeated_append(self, tmp_path: Path) -> None:
        csv_path = tmp_path / "ab-benchmark-results.csv"
        row = ab_benchmark.BenchmarkRow(
            date="2026-05-14",
            project="llm-mem",
            task="task one",
            run_a_tokens=100,
            run_b_tokens=100,
            delta_pct=0.0,
            run_a_reads="",
            run_b_reads="",
            citations="",
        )
        ab_benchmark.append_csv_row(csv_path, row)
        ab_benchmark.append_csv_row(csv_path, row)

        lines = csv_path.read_text().splitlines()
        assert lines.count(",".join(ab_benchmark.CSV_HEADER)) == 1
        assert len(lines) == 3  # header + 2 data rows

    def test_blank_reads_when_transcript_unavailable(self, tmp_path: Path) -> None:
        csv_path = tmp_path / "ab-benchmark-results.csv"
        row = ab_benchmark.BenchmarkRow(
            date="2026-05-14",
            project="llm-mem",
            task="task one",
            run_a_tokens=100,
            run_b_tokens=120,
            delta_pct=ab_benchmark.compute_delta_pct(100, 120),
            run_a_reads="",
            run_b_reads="",
            citations="",
        )
        ab_benchmark.append_csv_row(csv_path, row)
        with csv_path.open(newline="") as f:
            rows = list(csv.reader(f))
        assert rows[1][6] == ""  # run_a_reads
        assert rows[1][7] == ""  # run_b_reads


class TestCountTranscriptReads:
    def test_counts_only_read_tool_calls(self, tmp_path: Path) -> None:
        transcript = tmp_path / "session.jsonl"
        lines = [
            {
                "message": {
                    "content": [
                        {"type": "tool_use", "name": "Read", "input": {"file_path": "a.py"}}
                    ]
                }
            },
            {
                "message": {
                    "content": [
                        {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}
                    ]
                }
            },
            {
                "message": {
                    "content": [
                        {"type": "text", "text": "hi"},
                        {"type": "tool_use", "name": "Read", "input": {"file_path": "b.py"}},
                    ]
                }
            },
        ]
        transcript.write_text("\n".join(json.dumps(entry) for entry in lines))
        assert ab_benchmark.count_transcript_reads(transcript) == 2

    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        assert ab_benchmark.count_transcript_reads(tmp_path / "nope.jsonl") is None


class TestMain:
    def test_pairs_flag_limits_tasks_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tasks_path = tmp_path / "tasks.json"
        tasks_path.write_text(
            json.dumps(
                [
                    {"id": "t1", "prompt": "Explain 1"},
                    {"id": "t2", "prompt": "Explain 2"},
                    {"id": "t3", "prompt": "Explain 3"},
                ]
            )
        )
        csv_path = tmp_path / "results.csv"
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        calls: list[bool] = []

        def fake_run_claude(prompt: str, cwd: Path, *, disable_mcp: bool) -> object:
            calls.append(disable_mcp)
            return ab_benchmark.ClaudeRunResult(
                total_tokens=100 if disable_mcp else 80,
                input_tokens=10,
                output_tokens=10,
                session_id=None,
                reads=None,
            )

        monkeypatch.setattr(ab_benchmark, "run_claude", fake_run_claude)

        exit_code = ab_benchmark.main(
            [
                "--tasks",
                str(tasks_path),
                "--project",
                str(project_dir),
                "--pairs",
                "1",
                "--csv",
                str(csv_path),
            ]
        )

        assert exit_code == 0
        assert calls == [False, True]  # run A then run B, for exactly one pair
        with csv_path.open(newline="") as f:
            rows = list(csv.reader(f))
        assert len(rows) == 2  # header + 1 data row
