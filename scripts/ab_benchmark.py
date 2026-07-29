#!/usr/bin/env python3
"""A/B benchmark harness: does callmem actually save tokens?

For each task, runs the same prompt twice as a headless Claude Code
session -- once with callmem's MCP server active (run A) and once with it
disabled via `--strict-mcp-config --mcp-config '{"mcpServers":{}}'` (run
B) -- then appends a comparison row to docs/ab-benchmark-results.csv.

See docs/ab-benchmark.md for the measurement philosophy and CSV schema.
This script is built and unit-tested here; running it against real
sessions happens separately (it needs a live `claude` CLI and MCP setup).
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

CSV_HEADER = [
    "date",
    "project",
    "task",
    "run_a_tokens",
    "run_b_tokens",
    "delta_pct",
    "run_a_reads",
    "run_b_reads",
    "citations",
]

# Empty MCP server set -- disables callmem for run B. Flags confirmed
# against `claude --help`: --strict-mcp-config, --mcp-config <configs...>.
DISABLED_MCP_CONFIG = '{"mcpServers":{}}'


class TaskFileError(ValueError):
    """Raised when the task-list JSON file is missing or malformed."""


class ClaudeOutputError(ValueError):
    """Raised when a claude invocation fails or its JSON output doesn't
    have the documented usage/cost shape. Always includes the raw payload
    so a schema drift is loud and debuggable rather than silently wrong.
    """


@dataclass(frozen=True)
class BenchTask:
    id: str
    prompt: str


@dataclass(frozen=True)
class ClaudeRunResult:
    total_tokens: int
    input_tokens: int
    output_tokens: int
    session_id: str | None
    reads: int | None


@dataclass(frozen=True)
class BenchmarkRow:
    date: str
    project: str
    task: str
    run_a_tokens: int
    run_b_tokens: int
    delta_pct: float
    run_a_reads: int | str
    run_b_reads: int | str
    citations: str

    def as_csv_row(self) -> list[str]:
        return [
            self.date,
            self.project,
            self.task,
            str(self.run_a_tokens),
            str(self.run_b_tokens),
            f"{self.delta_pct:.1f}",
            str(self.run_a_reads),
            str(self.run_b_reads),
            self.citations,
        ]


def load_tasks(path: Path) -> list[BenchTask]:
    """Load and validate a task-list JSON file: `[{"id": ..., "prompt": ...}, ...]`."""
    try:
        text = path.read_text()
    except FileNotFoundError:
        raise TaskFileError(f"task file not found: {path}") from None

    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise TaskFileError(f"task file is not valid JSON: {path}: {exc}") from exc

    if not isinstance(raw, list) or not raw:
        raise TaskFileError(f"task file must contain a non-empty JSON array: {path}")

    tasks = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise TaskFileError(f"task {i} is not an object: {entry!r}")
        task_id = entry.get("id")
        prompt = entry.get("prompt")
        if not isinstance(task_id, str) or not task_id:
            raise TaskFileError(f"task {i} missing non-empty string 'id': {entry!r}")
        if not isinstance(prompt, str) or not prompt:
            raise TaskFileError(f"task {i} missing non-empty string 'prompt': {entry!r}")
        tasks.append(BenchTask(id=task_id, prompt=prompt))
    return tasks


def parse_claude_json(raw: str) -> ClaudeRunResult:
    """Parse the top-level result of `claude -p ... --output-format json`.

    Tolerant of extra/unknown fields; fails loudly with the raw payload if
    the documented `usage.input_tokens` / `usage.output_tokens` fields
    aren't present.
    """
    try:
        payload: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ClaudeOutputError(
            f"claude output is not valid JSON: {exc}\nraw payload:\n{raw}"
        ) from exc

    if not isinstance(payload, dict):
        raise ClaudeOutputError(f"claude output is not a JSON object\nraw payload:\n{raw}")

    usage = payload.get("usage")
    if not isinstance(usage, dict):
        raise ClaudeOutputError(
            f"claude output has no 'usage' object at the top level\nraw payload:\n{raw}"
        )

    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    if not isinstance(input_tokens, int) or not isinstance(output_tokens, int):
        raise ClaudeOutputError(
            "claude 'usage' is missing integer 'input_tokens'/'output_tokens'"
            f"\nraw payload:\n{raw}"
        )

    cache_creation = usage.get("cache_creation_input_tokens", 0)
    cache_read = usage.get("cache_read_input_tokens", 0)
    cache_creation = cache_creation if isinstance(cache_creation, int) else 0
    cache_read = cache_read if isinstance(cache_read, int) else 0

    session_id = payload.get("session_id")
    if not isinstance(session_id, str):
        session_id = None

    return ClaudeRunResult(
        total_tokens=input_tokens + output_tokens + cache_creation + cache_read,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        session_id=session_id,
        reads=None,
    )


def build_claude_command(prompt: str, *, disable_mcp: bool) -> list[str]:
    """Build the `claude -p ...` argv for run A (memory enabled) or run B (disabled)."""
    cmd = ["claude", "-p", prompt, "--output-format", "json"]
    if disable_mcp:
        cmd += ["--strict-mcp-config", "--mcp-config", DISABLED_MCP_CONFIG]
    return cmd


def run_claude(prompt: str, cwd: Path, *, disable_mcp: bool) -> ClaudeRunResult:
    """Invoke claude headless from `cwd` and parse its JSON result."""
    cmd = build_claude_command(prompt, disable_mcp=disable_mcp)
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise ClaudeOutputError(
            f"claude exited with status {proc.returncode}\nstderr:\n{proc.stderr}"
        )
    return parse_claude_json(proc.stdout)


def count_transcript_reads(transcript_path: Path) -> int | None:
    """Best-effort count of `Read` tool calls in a Claude Code transcript (.jsonl).

    Returns None if the transcript is missing or unparseable -- callers
    should leave the corresponding CSV field blank in that case.
    """
    if not transcript_path.exists():
        return None
    count = 0
    try:
        for line in transcript_path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            entry = json.loads(line)
            content = entry.get("message", {}).get("content", [])
            if not isinstance(content, list):
                continue
            for block in content:
                if (
                    isinstance(block, dict)
                    and block.get("type") == "tool_use"
                    and block.get("name") == "Read"
                ):
                    count += 1
    except (json.JSONDecodeError, AttributeError, TypeError):
        return None
    return count


def find_transcript(project_path: Path, session_id: str) -> Path | None:
    """Locate a Claude Code session transcript by project path + session id."""
    encoded = str(project_path.resolve()).replace("/", "-")
    candidate = Path.home() / ".claude" / "projects" / encoded / f"{session_id}.jsonl"
    return candidate if candidate.exists() else None


def compute_delta_pct(run_a_tokens: int, run_b_tokens: int) -> float:
    """Percent tokens saved by run A (callmem) vs run B (no callmem).

    Positive means callmem saved tokens: (B - A) / B * 100.
    """
    if run_b_tokens == 0:
        return 0.0
    return (run_b_tokens - run_a_tokens) / run_b_tokens * 100


def _reads_for(result: ClaudeRunResult, project_path: Path) -> int | str:
    if not result.session_id:
        return ""
    transcript = find_transcript(project_path, result.session_id)
    if transcript is None:
        return ""
    reads = count_transcript_reads(transcript)
    return reads if reads is not None else ""


def run_task_pair(
    task: BenchTask, project_path: Path, project_name: str, date: str
) -> BenchmarkRow:
    """Run A (enabled) then run B (disabled) for one task and build its CSV row."""
    result_a = run_claude(task.prompt, project_path, disable_mcp=False)
    result_b = run_claude(task.prompt, project_path, disable_mcp=True)

    return BenchmarkRow(
        date=date,
        project=project_name,
        task=task.id,
        run_a_tokens=result_a.total_tokens,
        run_b_tokens=result_b.total_tokens,
        delta_pct=compute_delta_pct(result_a.total_tokens, result_b.total_tokens),
        run_a_reads=_reads_for(result_a, project_path),
        run_b_reads=_reads_for(result_b, project_path),
        citations="",
    )


def append_csv_row(csv_path: Path, row: BenchmarkRow) -> None:
    """Append a row to the results CSV, writing the header first if new."""
    is_new = not csv_path.exists()
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("a", newline="") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow(CSV_HEADER)
        writer.writerow(row.as_csv_row())


def summarize(rows: list[BenchmarkRow]) -> str:
    """Per-task line plus a median delta_pct summary line."""
    lines = [
        f"{row.task}: A={row.run_a_tokens} B={row.run_b_tokens} delta={row.delta_pct:.1f}%"
        for row in rows
    ]
    if rows:
        median = statistics.median(row.delta_pct for row in rows)
        lines.append(f"median delta_pct: {median:.1f}%")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", type=Path, required=True, help="Path to task-list JSON")
    parser.add_argument(
        "--project", type=Path, required=True, help="Project directory to run claude in"
    )
    parser.add_argument(
        "--pairs",
        type=int,
        default=None,
        help="Number of task pairs to run (default: all tasks in the file)",
    )
    parser.add_argument(
        "--csv",
        type=Path,
        default=Path("docs/ab-benchmark-results.csv"),
        help="Results CSV path (appended to)",
    )
    args = parser.parse_args(argv)

    tasks = load_tasks(args.tasks)
    pairs = args.pairs if args.pairs is not None else len(tasks)
    if pairs < 1:
        parser.error("--pairs must be >= 1")
    tasks = tasks[:pairs]

    project_name = args.project.resolve().name
    date = datetime.now().strftime("%Y-%m-%d")

    rows = []
    for task in tasks:
        row = run_task_pair(task, args.project, project_name, date)
        append_csv_row(args.csv, row)
        rows.append(row)
        print(f"{task.id}: A={row.run_a_tokens} B={row.run_b_tokens} delta={row.delta_pct:.1f}%")

    if rows:
        print(summarize(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
