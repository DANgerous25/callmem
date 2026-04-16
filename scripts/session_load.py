#!/usr/bin/env python3
"""Load and display all bootstrap memory files.

Usage:
    python scripts/session_load.py            # Print all memory files
    python scripts/session_load.py --brief    # Print just SESSION.md
    python scripts/session_load.py --json     # Output as JSON (for piping to agents)

This is the bootstrap memory system used while llm-mem itself is being built.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
MEMORY_DIR = PROJECT_ROOT / ".llm-mem"

MEMORY_FILES = {
    "session": MEMORY_DIR / "SESSION.md",
    "decisions": MEMORY_DIR / "DECISIONS.md",
    "todo": MEMORY_DIR / "TODO.md",
}


def load_file(path: Path) -> str | None:
    """Load a file, return None if it doesn't exist."""
    if path.exists():
        return path.read_text().strip()
    return None


def print_divider(title: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Load bootstrap memory files")
    parser.add_argument("--brief", action="store_true", help="Show only SESSION.md")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    if not MEMORY_DIR.exists():
        print("No .llm-mem directory found. Run 'llm-mem init' or create it manually.")
        sys.exit(1)

    if args.json:
        data = {}
        for name, path in MEMORY_FILES.items():
            data[name] = load_file(path)
        print(json.dumps(data, indent=2))
        return

    if args.brief:
        content = load_file(MEMORY_FILES["session"])
        if content:
            print(content)
        else:
            print("No session summary found. Run 'python scripts/session_save.py' after your first session.")
        return

    # Full display
    found_any = False
    for name, path in MEMORY_FILES.items():
        content = load_file(path)
        if content:
            found_any = True
            print_divider(f"{name.upper()} — {path.name}")
            print(content)

    if not found_any:
        print("No memory files found in .llm-mem/")
        print("Bootstrap memory files:")
        print("  .llm-mem/SESSION.md   — Last session summary")
        print("  .llm-mem/DECISIONS.md — Design decisions log")
        print("  .llm-mem/TODO.md      — Current task list")
        print("\nThese will be created as you work. See AGENTS.md for the workflow.")


if __name__ == "__main__":
    main()
