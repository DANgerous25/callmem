#!/usr/bin/env python3
"""Save a session summary to .llm-mem/SESSION.md.

Usage:
    python scripts/session_save.py                    # Interactive prompt
    python scripts/session_save.py --from-git          # Auto-generate from recent git history
    python scripts/session_save.py --from-git --hours 4  # Git history from last 4 hours

This is the bootstrap memory system used while llm-mem itself is being built.
Once llm-mem is functional, this script becomes obsolete.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
MEMORY_DIR = PROJECT_ROOT / ".llm-mem"
SESSION_FILE = MEMORY_DIR / "SESSION.md"


def get_git_log(hours: int = 8) -> str:
    """Get recent git commits as a summary."""
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    try:
        result = subprocess.run(
            ["git", "log", f"--since={since}", "--oneline", "--no-merges"],
            capture_output=True,
            text=True,
            cwd=PROJECT_ROOT,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return "(no recent commits)"
        return result.stdout.strip()
    except FileNotFoundError:
        return "(git not available)"


def get_git_diff_stat() -> str:
    """Get uncommitted changes summary."""
    try:
        result = subprocess.run(
            ["git", "diff", "--stat", "HEAD"],
            capture_output=True,
            text=True,
            cwd=PROJECT_ROOT,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return "(no uncommitted changes)"
        return result.stdout.strip()
    except FileNotFoundError:
        return "(git not available)"


def get_test_status() -> str:
    """Run tests and capture summary."""
    try:
        result = subprocess.run(
            ["python", "-m", "pytest", "tests/", "-v", "--tb=no", "-q"],
            capture_output=True,
            text=True,
            cwd=PROJECT_ROOT,
            timeout=60,
        )
        lines = result.stdout.strip().split("\n")
        # Return just the summary line (e.g., "19 passed in 0.14s")
        for line in reversed(lines):
            if "passed" in line or "failed" in line:
                return line.strip()
        return result.stdout.strip()[-200:] if result.stdout else "(tests did not run)"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return "(could not run tests)"


def generate_from_git(hours: int) -> str:
    """Auto-generate a session summary from git history and test status."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    commits = get_git_log(hours)
    uncommitted = get_git_diff_stat()
    tests = get_test_status()

    return f"""# Session Summary

**Last updated:** {now}

## Recent Commits (last {hours}h)

```
{commits}
```

## Uncommitted Changes

```
{uncommitted}
```

## Test Status

```
{tests}
```

## What Was Done

(Auto-generated from git — edit this section to add context)

## What's Unfinished

(Fill in: what's left to do?)

## Problems Encountered

(Fill in: any blockers or surprises?)

## Next Steps

(Fill in: what should the next session start with?)
"""


def interactive_summary() -> str:
    """Prompt the user to write a session summary."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    commits = get_git_log(8)
    tests = get_test_status()

    print("=== Session Summary Generator ===")
    print(f"\nRecent commits:\n{commits}\n")
    print(f"Test status: {tests}\n")

    sections = {}
    prompts = {
        "done": "What did you accomplish this session?",
        "unfinished": "What's still unfinished?",
        "problems": "Any problems or blockers? (press Enter to skip)",
        "next": "What should the next session start with?",
    }

    for key, prompt in prompts.items():
        print(f"\n{prompt}")
        lines = []
        print("  (Enter a blank line to finish this section)")
        while True:
            line = input("  > ")
            if line == "":
                break
            lines.append(f"- {line}")
        sections[key] = "\n".join(lines) if lines else "(none)"

    return f"""# Session Summary

**Last updated:** {now}

## Recent Commits

```
{commits}
```

## Test Status

```
{tests}
```

## What Was Done

{sections['done']}

## What's Unfinished

{sections['unfinished']}

## Problems Encountered

{sections['problems']}

## Next Steps

{sections['next']}
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="Save a session summary")
    parser.add_argument(
        "--from-git",
        action="store_true",
        help="Auto-generate from git history (edit the result afterward)",
    )
    parser.add_argument(
        "--hours",
        type=int,
        default=8,
        help="Hours of git history to include (default: 8)",
    )
    args = parser.parse_args()

    MEMORY_DIR.mkdir(exist_ok=True)

    if args.from_git:
        content = generate_from_git(args.hours)
    else:
        content = interactive_summary()

    # Back up existing session file
    if SESSION_FILE.exists():
        backup = MEMORY_DIR / f"SESSION.{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.md"
        SESSION_FILE.rename(backup)
        print(f"Previous session backed up to {backup.name}")

    SESSION_FILE.write_text(content)
    print(f"\nSession summary saved to {SESSION_FILE}")

    if args.from_git:
        print("Review and edit the auto-generated sections before committing.")


if __name__ == "__main__":
    main()
