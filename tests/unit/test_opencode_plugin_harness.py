"""Runs the node behavioural harness for the OpenCode auto-briefing plugin.

The plugin itself is plain JS (src/callmem/templates/opencode/plugins/
auto-briefing.js) and isn't exercised by the Python test suite directly, so
this wraps tests/plugin/test_auto_briefing.mjs — a node script that imports
the plugin with a mocked @opencode-ai/plugin surface and asserts its
briefing-injection behaviour. Skips cleanly when node isn't available.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HARNESS = REPO_ROOT / "tests" / "plugin" / "test_auto_briefing.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not available")
def test_auto_briefing_plugin_harness() -> None:
    assert HARNESS.exists(), f"harness script missing: {HARNESS}"

    result = subprocess.run(  # noqa: S603
        ["node", str(HARNESS)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )

    if result.returncode != 0:
        sys.stderr.write(result.stdout)
        sys.stderr.write(result.stderr)
        pytest.fail(
            "node harness for auto-briefing.js failed "
            f"(exit {result.returncode}):\n{result.stdout}\n{result.stderr}"
        )
