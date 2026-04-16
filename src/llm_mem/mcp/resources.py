"""MCP resource definitions for llm-mem.

Resources are read-only data endpoints:
- memory://briefing
- memory://tasks
- memory://decisions
- memory://facts
- memory://session/current

Stub implementation — full resources come in a later WO.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mcp.server import Server


def register_resources(server: Server, engine: Any) -> None:
    """Register MCP resources (stubs for now)."""

    @server.list_resources()
    async def list_resources() -> list:
        return []
