"""Coding-tool integration helpers (OpenCode + Claude Code).

Shared between ``callmem.setup_wizard`` and ``callmem migrate`` so both keep
integration files in sync with the shipped templates.

Each helper is idempotent: it writes only when the destination is missing or
differs from the template, and returns the list of files it installed.
"""

from __future__ import annotations

import filecmp
import json
import subprocess
from pathlib import Path


def _find_templates_dir(kind: str) -> Path | None:
    """Locate ``templates/<kind>/`` shipped inside the ``callmem`` package."""
    candidate = Path(__file__).resolve().parent.parent / "templates" / kind
    return candidate if candidate.is_dir() else None


def detect_mcp_command(project: Path) -> list[str]:
    """Detect the best command to run the callmem MCP server.

    1. System python3 can import callmem → ``python3 -m callmem.mcp.server``.
    2. Inside the callmem source repo → ``uv run python -m ...``.
    3. Installed location reachable via python3 → ``uv run --directory <path>``.
    4. Fallback → ``python3`` best-effort.
    """
    try:
        result = subprocess.run(
            ["python3", "-c", "import callmem"],
            capture_output=True, timeout=5,
        )
        if result.returncode == 0:
            return ["python3", "-m", "callmem.mcp.server", "--project", "."]
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    if (project / "src" / "callmem").is_dir():
        return ["uv", "run", "python", "-m", "callmem.mcp.server", "--project", "."]

    try:
        result = subprocess.run(
            [
                "python3", "-c",
                "import callmem; from pathlib import Path; "
                "print(Path(callmem.__file__).parent.parent.parent)",
            ],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            callmem_dir = result.stdout.strip()
            return [
                "uv", "run", "--directory", callmem_dir,
                "python", "-m", "callmem.mcp.server", "--project", ".",
            ]
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    return ["python3", "-m", "callmem.mcp.server", "--project", "."]


def ensure_claude_code_mcp(project: Path, echo=print) -> list[str]:
    """Ensure ``.mcp.json`` has the callmem MCP server configured."""
    mcp_path = project / ".mcp.json"

    config: dict = {}
    if mcp_path.exists():
        try:
            config = json.loads(mcp_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            echo(f"  Warning: could not parse {mcp_path.name}, leaving unchanged")
            return []

    cmd = detect_mcp_command(project)
    command, args = cmd[0], cmd[1:]

    servers = config.get("mcpServers") or {}
    existing = servers.get("callmem", {})
    if existing.get("command") == command and existing.get("args") == args:
        return []

    servers["callmem"] = {"command": command, "args": args}
    config["mcpServers"] = servers
    mcp_path.write_text(json.dumps(config, indent=2) + "\n")
    action = "Updated" if existing else "Wrote"
    echo(f"  {action} callmem MCP server in {mcp_path.name}")
    return [mcp_path.name]


def _install_templates(
    mapping: list[tuple[Path, Path]],
    echo=print,
    label: str = "files",
    dry_run: bool = False,
) -> list[str]:
    installed: list[str] = []
    for src, dst in mapping:
        if not src.exists():
            continue
        if not dst.exists() or not filecmp.cmp(src, dst, shallow=False):
            if not dry_run:
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
            installed.append(dst.name)
    if installed:
        verb = "Would install" if dry_run else "Installed"
        echo(f"  {verb} {label}: {', '.join(installed)}")
    return installed


def ensure_opencode_plugin(
    project: Path, echo=print, dry_run: bool = False,
) -> list[str]:
    """Install OpenCode auto-briefing plugin + /briefing command."""
    templates_dir = _find_templates_dir("opencode")
    if templates_dir is None:
        return []
    mapping = [
        (templates_dir / "plugins" / "auto-briefing.js",
         project / ".opencode" / "plugins" / "auto-briefing.js"),
        (templates_dir / "commands" / "briefing.md",
         project / ".opencode" / "commands" / "briefing.md"),
    ]
    return _install_templates(
        mapping, echo=echo, label="OpenCode files", dry_run=dry_run,
    )


def ensure_claude_code_commands(
    project: Path, echo=print, dry_run: bool = False,
) -> list[str]:
    """Install Claude Code /briefing slash command."""
    templates_dir = _find_templates_dir("claude")
    if templates_dir is None:
        return []
    mapping = [
        (templates_dir / "commands" / "briefing.md",
         project / ".claude" / "commands" / "briefing.md"),
    ]
    return _install_templates(
        mapping, echo=echo, label="Claude Code commands", dry_run=dry_run,
    )
