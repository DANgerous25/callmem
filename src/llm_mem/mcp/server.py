"""Compatibility shim for ``llm_mem.mcp.server`` — redirects to ``callmem.mcp.server``.

This lets existing ``.mcp.json`` / ``opencode.json`` configs invoking
``python -m llm_mem.mcp.server`` keep working after the rename. Update those
configs to use ``callmem.mcp.server`` when convenient.
"""

from callmem.mcp.server import *  # noqa: F401, F403
from callmem.mcp.server import main

if __name__ == "__main__":
    main()
