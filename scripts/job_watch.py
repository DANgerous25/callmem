#!/usr/bin/env python3
"""Watch callmem job queue progress with live ETA."""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path


def show_progress(db_path: str) -> None:
    db = sqlite3.connect(db_path)
    counts = dict(db.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status").fetchall())
    pending = counts.get("pending", 0)
    completed = counts.get("completed", 0)
    running = counts.get("running", 0)
    failed = counts.get("failed", 0)
    total = pending + completed + running + failed

    recent = db.execute(
        "SELECT MIN(completed_at), MAX(completed_at), COUNT(*) "
        "FROM jobs WHERE status='completed' "
        "AND completed_at > datetime('now', '-10 minutes')"
    ).fetchone()

    rate = ""
    eta = ""
    if recent and recent[2] > 1 and recent[0] and recent[1]:
        t0 = datetime.fromisoformat(recent[0])
        t1 = datetime.fromisoformat(recent[1])
        secs = (t1 - t0).total_seconds()
        if secs > 0:
            per_min = recent[2] / (secs / 60)
            mins_left = pending / per_min if per_min > 0 else 0
            rate = f"{per_min:.1f} jobs/min"
            if mins_left > 60:
                eta = f"{mins_left / 60:.1f} hours"
            else:
                eta = f"{mins_left:.0f} min"

    pct = int(completed / total * 100) if total > 0 else 0
    bar_len = 30
    filled = int(bar_len * completed / total) if total > 0 else 0
    bar = "█" * filled + "░" * (bar_len - filled)

    print(f"[{bar}] {pct}%")
    print(f"Completed: {completed}/{total}")
    print(f"Pending:   {pending}")
    print(f"Running:   {running}")
    if failed:
        print(f"Failed:    {failed}")
    if rate:
        print(f"Rate:      {rate}")
    if eta:
        print(f"ETA:       {eta}")
    db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Watch callmem job queue progress")
    parser.add_argument("--project", "-p", type=str, default=".", help="Project root directory")
    parser.add_argument("--interval", "-n", type=int, default=30, help="Refresh interval in seconds")
    parser.add_argument("--once", action="store_true", help="Show once and exit (no watch loop)")
    args = parser.parse_args()

    db_path = str(Path(args.project).resolve() / ".callmem" / "memory.db")
    if not Path(db_path).exists():
        print(f"Database not found: {db_path}", file=sys.stderr)
        sys.exit(1)

    if args.once:
        show_progress(db_path)
        return

    try:
        while True:
            # Clear screen for watch effect
            print("\033[2J\033[H", end="")
            print(f"callmem job queue — {datetime.now().strftime('%H:%M:%S')}")
            print()
            show_progress(db_path)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print()


if __name__ == "__main__":
    main()
