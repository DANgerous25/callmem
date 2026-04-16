"""MCP server entry point.

Launches llm-mem as an MCP server over stdio (default) or SSE transport.
This is what OpenCode spawns when configured with:

    "command": ["python", "-m", "llm_mem.mcp.server", "--project", "."]
"""

from __future__ import annotations

# Placeholder — implementation in WO-05
# This file exists so the module path is importable and the
# MCP server entry point is clear.

def main() -> None:
    """Start the MCP server."""
    # TODO: WO-05 implementation
    # 1. Parse --project and --transport args
    # 2. Initialize Database and MemoryEngine
    # 3. Register MCP tools from tools.py
    # 4. Register MCP resources from resources.py
    # 5. Start server on configured transport
    raise NotImplementedError("MCP server not yet implemented — see WO-05")


if __name__ == "__main__":
    main()
