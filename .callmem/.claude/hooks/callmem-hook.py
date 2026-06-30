#!/usr/bin/env python3
"""Claude Code hook → callmem daemon bridge.

Reads Claude Code hook JSON from stdin and POSTs to the callmem daemon's
HTTP ingest endpoint.  Invoked by hooks.json for each lifecycle event.

Usage: callmem-hook <event-type>
  (reads JSON payload from stdin)
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path


def _get_port() -> int:
    """Read UI port from callmem.toml or default."""
    project = Path(os.environ.get("CLAUDE_PROJECT_DIR", ".")).resolve()
    toml = project / ".callmem" / "callmem.toml"
    if toml.exists():
        for line in toml.read_text().splitlines():
            line = line.strip()
            if line.startswith("port") and "=" in line:
                try:
                    return int(line.split("=", 1)[1].strip())
                except ValueError:
                    pass
    return 9097


def _ingest(events: list[dict], session_action: str | None = None,
            session_id: str | None = None, agent_name: str | None = None) -> None:
    """POST events to the callmem daemon."""
    port = _get_port()
    url = f"http://127.0.0.1:{port}/api/ingest"
    payload = {
        "events": events,
        "session_action": session_action,
        "session_id": session_id,
        "agent_name": agent_name,
    }
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            resp.read()
    except Exception:
        pass  # daemon down — polling adapter is fallback


def _truncate(text: str, limit: int = 5000) -> str:
    return text[:limit] if len(text) > limit else text


def main() -> None:
    if len(sys.argv) < 2:
        return
    event_type = sys.argv[1]

    raw = sys.stdin.read()
    if not raw:
        return

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return

    agent_name = "claude-code"

    if event_type == "SessionStart":
        _ingest([], session_action="start", agent_name=agent_name)
        return

    if event_type == "SessionEnd":
        _ingest([], session_action="end")
        return

    if event_type == "UserPromptSubmit":
        prompt = data.get("prompt", "")
        if prompt:
            _ingest([{
                "type": "prompt",
                "content": _truncate(prompt),
                "timestamp": data.get("timestamp"),
            }])
        return

    if event_type == "PostToolUse":
        tool = data.get("tool_name") or data.get("tool", "unknown")
        tool_input = data.get("tool_input") or data.get("input") or {}

        if tool in ("Read", "read", "Write", "write", "Edit", "edit", "MultiEdit"):
            path = (
                tool_input.get("file_path")
                or tool_input.get("filePath")
                or tool_input.get("path", "")
            )
            if path:
                _ingest([{
                    "type": "file_change",
                    "content": f"modified: {path}",
                }])
                return

        args = json.dumps(tool_input)[:200] if tool_input else ""
        content = f"{tool}({args})" if args else tool
        _ingest([{
            "type": "tool_call",
            "content": _truncate(content, 500),
        }])
        return

    if event_type == "Stop":
        _ingest([], session_action="end")
        return


if __name__ == "__main__":
    main()
