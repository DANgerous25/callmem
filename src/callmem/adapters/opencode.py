"""OpenCode SSE event listener adapter.

Connects to OpenCode's SSE event stream and translates events
into callmem ingest calls. Handles reconnection on server restart.
"""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING, Any

from callmem.models.events import EventInput

if TYPE_CHECKING:
    from callmem.core.engine import MemoryEngine

logger = logging.getLogger(__name__)

EVENT_TYPE_MAP: dict[str, str] = {
    "message.created": "message",
    "tool.invoked": "tool",
    "file.changed": "file_change",
    "session.created": "session_lifecycle",
    "session.completed": "session_lifecycle",
}

RECONNECT_DELAY = 5


class OpenCodeAdapter:
    """Listens to OpenCode SSE events and ingests them into callmem."""

    def __init__(
        self,
        engine: MemoryEngine,
        opencode_url: str = "http://localhost:4096",
    ) -> None:
        self.engine = engine
        self.opencode_url = opencode_url.rstrip("/")
        self._running = False

    def process_event(self, event: dict[str, Any]) -> EventInput | None:
        """Translate an OpenCode SSE event into an callmem EventInput.

        Returns None if the event is not relevant.
        """
        event_type = event.get("type", "")
        data = event.get("data", {})

        if event_type == "message.created":
            return self._map_message(data)
        elif event_type == "tool.invoked":
            return self._map_tool_call(data)
        elif event_type == "file.changed":
            return self._map_file_change(data)
        elif event_type == "session.created":
            self.engine.start_session(agent_name="opencode")
            return None
        elif event_type == "session.completed":
            active = self.engine.get_active_session()
            if active is not None:
                self.engine.end_session(active.id)
            return None

        return None

    def _map_message(self, data: dict[str, Any]) -> EventInput | None:
        role = data.get("role", "")
        content = data.get("content", "")
        if not content:
            return None

        event_type = "prompt" if role == "user" else "response"
        return EventInput(type=event_type, content=content)

    def _map_tool_call(self, data: dict[str, Any]) -> EventInput | None:
        tool_name = data.get("tool", "unknown")
        args = data.get("args", {})
        args_summary = json.dumps(args)[:200] if args else ""
        content = f"{tool_name}({args_summary})" if args_summary else tool_name
        return EventInput(type="tool_call", content=content)

    def _map_file_change(self, data: dict[str, Any]) -> EventInput | None:
        path = data.get("path", "unknown")
        change_type = data.get("change", "modified")
        content = f"{change_type}: {path}"
        return EventInput(type="file_change", content=content)

    def run(self) -> None:
        """Connect to OpenCode SSE stream and process events.

        Reconnects automatically on disconnect. Blocks until stopped.
        """
        import httpx

        self._running = True
        logger.info("Connecting to OpenCode at %s", self.opencode_url)

        while self._running:
            try:
                with httpx.stream(
                    "GET",
                    f"{self.opencode_url}/event",
                    timeout=httpx.Timeout(None, connect=10.0),
                ) as response:
                    response.raise_for_status()
                    logger.info("Connected to OpenCode SSE stream")

                    for line in response.iter_lines():
                        if not self._running:
                            break
                        if not line:
                            continue
                        if line.startswith("data: "):
                            payload = line[6:]
                            try:
                                event = json.loads(payload)
                            except json.JSONDecodeError:
                                continue
                            self._handle_event(event)

            except (httpx.ConnectError, httpx.TimeoutException) as exc:
                logger.warning("OpenCode connection lost: %s", exc)
            except httpx.HTTPStatusError as exc:
                logger.error("OpenCode HTTP error: %s", exc)

            if self._running:
                logger.info("Reconnecting in %ds...", RECONNECT_DELAY)
                time.sleep(RECONNECT_DELAY)

    def stop(self) -> None:
        """Signal the adapter to stop."""
        self._running = False

    def _handle_event(self, event: dict[str, Any]) -> None:
        """Process a single SSE event and ingest if relevant."""
        event_input = self.process_event(event)
        if event_input is not None:
            try:
                self.engine.ingest([event_input])
            except Exception as exc:
                logger.error("Failed to ingest event: %s", exc)
