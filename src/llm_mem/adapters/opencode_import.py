"""OpenCode session history importer.

Reads OpenCode session JSON files from disk and ingests them
into llm-mem as historical sessions. Separate from the live SSE adapter.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from llm_mem.models.events import EventInput

if TYPE_CHECKING:
    from llm_mem.core.engine import MemoryEngine

logger = logging.getLogger(__name__)

DEFAULT_SESSION_DIR = Path.home() / ".local" / "share" / "opencode"


def discover_session_files(session_dir: Path) -> list[Path]:
    """Find all JSON session files under the given directory."""
    if not session_dir.is_dir():
        return []
    return sorted(session_dir.rglob("*.json"))


def read_session_file(path: Path) -> dict[str, Any] | None:
    """Read and parse a single session JSON file.

    Returns None if the file cannot be parsed.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Skipping %s: %s", path, exc)
    return None


def _extract_content(message: dict[str, Any]) -> str:
    """Extract text content from a message, handling both flat and structured formats."""
    content = message.get("content", "")
    if isinstance(content, str):
        return content

    # Handle structured parts array
    if isinstance(content, list):
        texts = []
        for part in content:
            if isinstance(part, str):
                texts.append(part)
            elif isinstance(part, dict):
                texts.append(part.get("text", part.get("content", "")))
        return "\n".join(t for t in texts if t)

    return str(content) if content else ""


def _map_message(message: dict[str, Any]) -> list[EventInput]:
    """Map a single OpenCode message to llm-mem EventInput(s)."""
    events: list[EventInput] = []
    role = message.get("role", "")
    content = _extract_content(message)

    if content:
        if role == "user":
            events.append(EventInput(type="prompt", content=content))
        elif role == "assistant":
            events.append(EventInput(type="response", content=content))

    # Map tool calls if present
    tool_calls = message.get("tool_calls", [])
    if isinstance(tool_calls, list):
        for tc in tool_calls:
            name = tc.get("function", {}).get("name", tc.get("name", "unknown"))
            args = tc.get("function", {}).get("arguments", tc.get("args", ""))
            if isinstance(args, dict):
                args = json.dumps(args)[:200]
            elif isinstance(args, str) and len(args) > 200:
                args = args[:200]
            tc_content = f"{name}({args})" if args else name
            events.append(EventInput(type="tool_call", content=tc_content))

    # Map file changes if present in parts
    parts = message.get("parts", [])
    if isinstance(parts, list):
        for part in parts:
            if isinstance(part, dict) and part.get("type") == "file_change":
                path = part.get("path", "unknown")
                change = part.get("change", "modified")
                events.append(
                    EventInput(type="file_change", content=f"{change}: {path}")
                )

    return events


def import_session(
    engine: MemoryEngine,
    session_data: dict[str, Any],
) -> dict[str, Any]:
    """Import a single OpenCode session into llm-mem.

    Returns a summary dict with session_id, event_count, and any errors.
    """
    title = session_data.get("title", "imported session")
    messages = session_data.get("messages", [])

    session = engine.start_session(agent_name="opencode")

    event_count = 0
    errors: list[str] = []

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        try:
            inputs = _map_message(msg)
            if inputs:
                stored = engine.ingest(inputs)
                event_count += len(stored)
        except Exception as exc:
            errors.append(str(exc))
            logger.warning("Error processing message: %s", exc)

    try:
        engine.end_session(session.id, note=title)
    except Exception as exc:
        errors.append(f"Failed to end session: {exc}")
        logger.warning("Failed to end session %s: %s", session.id, exc)

    return {
        "session_id": session.id,
        "source_id": session_data.get("id", "unknown"),
        "title": title,
        "event_count": event_count,
        "errors": errors,
    }


def import_sessions(
    engine: MemoryEngine,
    session_dir: Path,
    session_id: str | None = None,
    import_all: bool = False,
    dry_run: bool = False,
) -> list[dict[str, Any]]:
    """Import OpenCode sessions from disk.

    Args:
        engine: The MemoryEngine instance.
        session_dir: Directory containing OpenCode session JSON files.
        session_id: If provided, only import the session matching this ID.
        import_all: If True, import all discovered sessions.
        dry_run: If True, report what would be imported without importing.

    Returns:
        List of result dicts, one per session processed.
    """
    files = discover_session_files(session_dir)
    if not files:
        return []

    results: list[dict[str, Any]] = []

    for path in files:
        data = read_session_file(path)
        if data is None:
            continue

        sid = data.get("id", path.stem)

        if session_id is not None and sid != session_id:
            continue

        if dry_run:
            msg_count = len(data.get("messages", []))
            results.append({
                "source_id": sid,
                "title": data.get("title", ""),
                "message_count": msg_count,
                "file": str(path),
                "dry_run": True,
            })
            continue

        if not import_all and session_id is None:
            # Without --all or --session-id, just list what's available
            msg_count = len(data.get("messages", []))
            results.append({
                "source_id": sid,
                "title": data.get("title", ""),
                "message_count": msg_count,
                "file": str(path),
                "dry_run": True,
            })
            continue

        result = import_session(engine, data)
        result["file"] = str(path)
        results.append(result)

    return results
