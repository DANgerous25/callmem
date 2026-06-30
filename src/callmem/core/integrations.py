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
import sys
from pathlib import Path


def _find_templates_dir(kind: str) -> Path | None:
    """Locate ``templates/<kind>/`` shipped inside the ``callmem`` package."""
    candidate = Path(__file__).resolve().parent.parent / "templates" / kind
    return candidate if candidate.is_dir() else None


def _venv_python(project: Path) -> Path | None:
    """Return ``project/.venv/bin/python`` (or ``Scripts/python.exe`` on Windows)
    if it exists, else None.
    """
    candidates = [
        project / ".venv" / "bin" / "python",
        project / ".venv" / "Scripts" / "python.exe",
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


def _can_import_callmem(python: Path | str) -> bool:
    try:
        result = subprocess.run(
            [str(python), "-c", "import callmem"],
            capture_output=True, timeout=5,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def detect_mcp_command(project: Path) -> list[str]:
    """Detect the best command to run the callmem MCP server.

    OpenCode and Claude Code spawn MCP subprocesses with cwd set to the
    project, and may auto-activate a project-local ``.venv/`` ahead of
    PATH. So a bare ``python3`` is unreliable — it can resolve to a venv
    interpreter that doesn't have callmem installed. We prefer absolute
    interpreter paths.

    Order of preference:

    1. ``<project>/.venv/bin/python`` — if it can import callmem. Most
       natural since the agent is already using that interpreter.
    2. ``sys.executable`` — the python running the wizard. Always has
       callmem (the wizard imports from it). Absolute path survives
       whatever the agent does to PATH when spawning the subprocess.
    """
    project_abs = str(project.resolve())

    venv_python = _venv_python(project)
    if venv_python is not None and _can_import_callmem(venv_python):
        return [
            str(venv_python), "-m", "callmem.mcp.server",
            "--project", project_abs,
        ]

    return [
        sys.executable, "-m", "callmem.mcp.server",
        "--project", project_abs,
    ]


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
    """Install OpenCode auto-briefing plugin, callmem capture plugin,
    /briefing command, and BRIEFING_INSTRUCTIONS.md."""
    templates_dir = _find_templates_dir("opencode")
    if templates_dir is None:
        return []
    mapping = [
        (templates_dir / "plugins" / "auto-briefing.js",
         project / ".opencode" / "plugins" / "auto-briefing.js"),
        (templates_dir / "plugins" / "callmem.js",
         project / ".opencode" / "plugins" / "callmem.js"),
        (templates_dir / "commands" / "briefing.md",
         project / ".opencode" / "commands" / "briefing.md"),
        (templates_dir / "BRIEFING_INSTRUCTIONS.md",
         project / ".opencode" / "BRIEFING_INSTRUCTIONS.md"),
    ]
    return _install_templates(
        mapping, echo=echo, label="OpenCode files", dry_run=dry_run,
    )


def ensure_claude_code_commands(
    project: Path, echo=print, dry_run: bool = False,
) -> list[str]:
    """Install Claude Code /briefing slash command and callmem capture hook."""
    templates_dir = _find_templates_dir("claude")
    if templates_dir is None:
        return []
    mapping = [
        (templates_dir / "commands" / "briefing.md",
         project / ".claude" / "commands" / "briefing.md"),
        (templates_dir / "hooks" / "callmem-hook.py",
         project / ".claude" / "hooks" / "callmem-hook.py"),
    ]
    installed = _install_templates(
        mapping, echo=echo, label="Claude Code files", dry_run=dry_run,
    )
    if not dry_run:
        hook = project / ".claude" / "hooks" / "callmem-hook.py"
        if hook.exists():
            hook.chmod(0o755)
    if not dry_run:
        _ensure_claude_code_hooks(project, echo=echo)
    return installed


CLAUDE_HOOK_EVENTS = [
    "SessionStart",
    "UserPromptSubmit",
    "PostToolUse",
    "Stop",
    "SessionEnd",
]


def _ensure_claude_code_hooks(project: Path, echo=print) -> None:
    """Register callmem hooks in Claude Code settings.json.

    Claude Code reads hooks from ~/.claude/settings.json. We add callmem
    hook entries for the lifecycle events we care about, using the
    project-local hook script. Idempotent — won't duplicate entries.
    """
    settings_path = Path.home() / ".claude" / "settings.json"
    if not settings_path.exists():
        return

    try:
        config = json.loads(settings_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        echo("  Warning: could not parse Claude Code settings.json")
        return

    hooks = config.setdefault("hooks", {})
    hook_script = str(project / ".claude" / "hooks" / "callmem-hook.py")
    python = sys.executable
    cmd = f"{python} {hook_script}"

    changed = False
    for event in CLAUDE_HOOK_EVENTS:
        entries = hooks.setdefault(event, [])

        already = any(
            any(
                "callmem-hook" in (h.get("command") or "")
                for h in entry.get("hooks", [])
            )
            for entry in entries
        )
        if already:
            continue

        entry = {
            "hooks": [
                {
                    "type": "command",
                    "command": cmd,
                    "async": event != "SessionStart",
                }
            ],
        }
        if event == "PostToolUse":
            entry["matcher"] = "*"
        entries.append(entry)
        changed = True

    if changed:
        settings_path.write_text(
            json.dumps(config, indent=2) + "\n", encoding="utf-8"
        )
        echo("  Registered callmem hooks in Claude Code settings.json")


def _silent(*_args: object, **_kwargs: object) -> None:
    pass


def check_integration_drift(
    project: Path,
    fix: bool = False,
    echo=_silent,
    check_opencode: bool = True,
    check_claude: bool = True,
) -> dict[str, list[str]]:
    """Detect stale or missing shipped-template integration files.

    Runs the per-tool ``ensure_*`` helpers in dry-run mode (or in repair mode
    when ``fix=True``) and returns a mapping of category → list of file
    basenames that drifted. Empty lists mean the category is clean.

    Args:
        project: project root containing ``.opencode/`` and/or ``.claude/``.
        fix: when True, repair drift in addition to reporting it.
        echo: callable used by the underlying helpers when they report drift.
            Defaults to a no-op so callers can format their own output.
        check_opencode: whether to check OpenCode integration files.
        check_claude: whether to check Claude Code integration files.

    Returns:
        ``{"opencode": [...], "claude_code": [...]}`` — only the categories
        actually checked appear in the dict.
    """
    result: dict[str, list[str]] = {}
    if check_opencode:
        result["opencode"] = ensure_opencode_plugin(
            project, echo=echo, dry_run=not fix,
        )
    if check_claude:
        result["claude_code"] = ensure_claude_code_commands(
            project, echo=echo, dry_run=not fix,
        )
    return result
