"""Compatibility shim — this package has been renamed to ``callmem``.

Existing ``.mcp.json`` / ``opencode.json`` configs that invoke
``python -m llm_mem.mcp.server`` will continue to work. Please update those
configs to use ``callmem.mcp.server`` — this shim will be removed in a future
release.
"""

import warnings

warnings.warn(
    "The `llm_mem` package has been renamed to `callmem`. "
    "Update your .mcp.json / opencode.json to use `callmem.mcp.server`.",
    DeprecationWarning,
    stacklevel=2,
)
