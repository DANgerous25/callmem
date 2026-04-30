"""CLI entry point for callmem."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import click

from callmem import __version__


@click.group()
@click.version_option(version=__version__, prog_name="callmem")
def main() -> None:
    """callmem: Persistent memory for coding agents."""


_SESSION_SUMMARY_SNIPPET = (
    "\n## Startup briefing\n\n"
    "At the **start of every session**, read `SESSION_SUMMARY.md` (in the project root) "
    "if it exists. It contains an auto-generated briefing with recent context, key "
    "entities, and open tasks from previous sessions.\n\n"
    "After reading it, present a brief summary to the user: greet them, state the "
    "project name, mention the most recent session activity, highlight any open TODOs "
    "or unresolved failures, and ask what they'd like to work on.\n"
)

_MCP_BLOCK_SNIPPET = (

    "\n## Memory (callmem)\n\n"
    "This project uses callmem for persistent memory via MCP tools.\n\n"
    "**Start of session:**\n"
    "1. Read `SESSION_SUMMARY.md` (if it exists) for an auto-generated briefing\n"
    "2. Call `mem_session_start` to register this session\n"
    "3. Present a brief summary: greet the user, mention recent activity, highlight open TODOs\n\n"
    "**During the session:**\n"
    "- When you make a design decision, call `mem_ingest` with type \"decision\"\n"
    "- When you identify a TODO, call `mem_ingest` with type \"todo\"\n"
    "- When you discover something notable, call `mem_ingest` with type \"discovery\"\n"
    "- When something fails unexpectedly, call `mem_ingest` with type \"failure\"\n"
    "- To recall past context, call `mem_search` with keywords\n"
    "- To see open tasks, call `mem_get_tasks`\n\n"
    "**Before re-reading a file you've worked on before:**\n"
    "- Call `mem_file_context` with the file path\n"
    "- If the returned timeline covers what you need, skip the raw read (saves tokens)\n"
    "- If you need exact line-level details, read the file normally\n\n"
    "**Long sessions (50+ messages):**\n"
    "- Every ~30 messages, call `mem_check_context` with your approximate message count\n"
    "- If it returns `compress_recommended`, summarize the oldest ~30 messages "
    "(preserve decisions/TODOs/failures verbatim) and call `mem_compress_context`\n"
    "- Replace the compressed span in your context with the returned marker; "
    "use `mem_search` to recall specifics\n\n"
    "**End of session:**\n"
    "- Call `mem_session_end` to trigger summary generation\n\n"
    "**Guidelines:**\n"
    "- Be specific in memory content (include file paths, function names, error messages)\n"
    "- Set priority on TODOs: high, medium, or low\n"
    "- Mark failures as resolved when you fix them\n"
    "- The system captures raw events automatically — focus on recording decisions and TODOs\n"
)

_MCP_SENTINELS = ("## Memory (callmem)", "mem_ingest", "mem_session_start")


def _ensure_agents_session_summary(agents_path: Path) -> None:
    """Patch an existing AGENTS.md to reference SESSION_SUMMARY.md if missing."""
    if not agents_path.exists():
        return
    content = agents_path.read_text(encoding="utf-8")
    if "SESSION_SUMMARY.md" in content:
        return
    content += _SESSION_SUMMARY_SNIPPET
    agents_path.write_text(content, encoding="utf-8")


def _ensure_agents_mcp_block(agents_path: Path) -> None:
    """Patch an existing AGENTS.md with callmem MCP tool usage instructions."""
    if not agents_path.exists():
        return
    content = agents_path.read_text(encoding="utf-8")
    if any(s in content for s in _MCP_SENTINELS):
        click.echo("  AGENTS.md already has callmem instructions")
        return
    content += _MCP_BLOCK_SNIPPET
    agents_path.write_text(content, encoding="utf-8")
    click.echo("  Patched AGENTS.md with callmem MCP tool instructions")


def _ensure_opencode_plugin(project: Path) -> None:
    """Install OpenCode auto-briefing plugin and /briefing command if missing or outdated."""
    import filecmp

    templates_dir = Path(__file__).parent.parent.parent / "templates" / "opencode"

    targets = [
        (
            templates_dir / "plugins" / "auto-briefing.js",
            project / ".opencode" / "plugins" / "auto-briefing.js",
        ),
        (
            templates_dir / "commands" / "briefing.md",
            project / ".opencode" / "commands" / "briefing.md",
        ),
    ]

    for src, dst in targets:
        if not src.exists():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        if not dst.exists() or not filecmp.cmp(src, dst, shallow=False):
            dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")


def _detect_mcp_command(project: Path) -> list[str]:
    """Detect the best command to run the callmem MCP server.

    Checks in order:
    1. System Python can import callmem → use python3 directly.
    2. We're inside the callmem project itself → use uv run.
    3. Find callmem install path → use uv run --directory.
    4. Fallback → use python3 (best-effort).
    """
    import subprocess

    # 1. Check if callmem is importable from system Python
    try:
        result = subprocess.run(
            ["python3", "-c", "import callmem"],
            capture_output=True, timeout=5,
        )
        if result.returncode == 0:
            return ["python3", "-m", "callmem.mcp.server", "--project", "."]
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # 2. Check if we're in the callmem project itself
    if (project / "src" / "callmem").is_dir():
        return ["uv", "run", "python", "-m", "callmem.mcp.server", "--project", "."]

    # 3. Fall back to uv with --directory pointing to callmem source
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

    # 4. Ultimate fallback
    return ["python3", "-m", "callmem.mcp.server", "--project", "."]


def _ensure_opencode_instructions(project: Path) -> None:
    """Ensure opencode.json has SESSION_SUMMARY.md and correct MCP config."""
    import json

    oc_path = None
    for name in ("opencode.json", ".opencode.json", "opencode.jsonc"):
        candidate = project / name
        if candidate.exists():
            oc_path = candidate
            break

    if oc_path is None:
        return

    try:
        oc_config = json.loads(oc_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return

    changed = False

    # Ensure MCP server command is correct
    mcp = oc_config.get("mcp", {})
    callmem_mcp = mcp.get("callmem", {})
    detected_cmd = _detect_mcp_command(project)
    if callmem_mcp.get("command") != detected_cmd:
        existed = "callmem" in mcp
        if "mcp" not in oc_config:
            oc_config["mcp"] = {}
        oc_config["mcp"]["callmem"] = {
            "type": "local",
            "command": detected_cmd,
            "enabled": True,
        }
        changed = True
        if existed:
            click.echo("Updated MCP server command in opencode.json")

    # Ensure SESSION_SUMMARY.md in instructions
    instructions = oc_config.get("instructions", [])
    if "SESSION_SUMMARY.md" not in instructions:
        instructions.append("SESSION_SUMMARY.md")
        oc_config["instructions"] = instructions
        changed = True

    if changed:
        oc_path.write_text(json.dumps(oc_config, indent=2) + "\n", encoding="utf-8")


def _ensure_claude_code_mcp(project: Path) -> None:
    """Ensure .mcp.json has callmem MCP server configured for Claude Code.

    Claude Code uses a split command/args schema (different from OpenCode's
    single-array command). Preserves any other MCP servers in the file.
    """
    import json

    mcp_path = project / ".mcp.json"

    config: dict = {}
    if mcp_path.exists():
        try:
            config = json.loads(mcp_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            click.echo(f"  Warning: could not parse {mcp_path.name}, leaving unchanged")
            return

    detected = _detect_mcp_command(project)
    command, args = detected[0], detected[1:]

    servers = config.get("mcpServers") or {}
    existing = servers.get("callmem", {})
    if existing.get("command") == command and existing.get("args") == args:
        click.echo("  .mcp.json already has callmem MCP server")
        return

    servers["callmem"] = {"command": command, "args": args}
    config["mcpServers"] = servers

    mcp_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    action = "Updated" if existing else "Added"
    click.echo(f"  {action} callmem MCP server in .mcp.json")


def _claude_md_is_separate_file(claude_path: Path, agents_path: Path) -> bool:
    """True if CLAUDE.md exists as a separate file (not a symlink to AGENTS.md)."""
    if not claude_path.exists() and not claude_path.is_symlink():
        return False
    if claude_path.is_symlink():
        try:
            target = claude_path.resolve(strict=False)
            return target != agents_path.resolve(strict=False)
        except OSError:
            return True
    return True


@main.command()
@click.option("--project", "-p", type=click.Path(path_type=Path), default=".")
def setup(project: Path) -> None:
    """Interactive setup wizard for a new or existing project."""
    import subprocess
    import sys

    script = Path(__file__).parent.parent.parent / "scripts" / "setup.py"
    if not script.exists():
        # Fallback: try relative to the project
        script = project / "scripts" / "setup.py"

    if script.exists():
        subprocess.run([sys.executable, str(script)], check=False)
    else:
        click.echo("Setup script not found. Run: python scripts/setup.py")


@main.command()
@click.option(
    "--project", "-p",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=".",
    help="Project root directory.",
)
def init(project: Path) -> None:
    """Initialize callmem for a project."""
    from callmem.core.config import generate_default_config
    from callmem.core.database import Database

    callmem_dir = project / ".callmem"
    callmem_dir.mkdir(exist_ok=True)

    db_path = callmem_dir / "memory.db"
    db = Database(db_path)
    db.initialize()

    config_path = callmem_dir / "config.toml"
    if not config_path.exists():
        config_path.write_text(generate_default_config(project.name))

    agents_template = Path(__file__).parent.parent.parent / "templates" / "AGENTS.md.template"
    agents_path = project / "AGENTS.md"
    if agents_template.exists() and not agents_path.exists():
        agents_path.write_text(agents_template.read_text())

    _ensure_agents_session_summary(agents_path)
    _ensure_agents_mcp_block(agents_path)

    claude_path = project / "CLAUDE.md"
    if _claude_md_is_separate_file(claude_path, agents_path):
        _ensure_agents_mcp_block(claude_path)

    _ensure_opencode_instructions(project)
    _ensure_opencode_plugin(project)
    _ensure_claude_code_mcp(project)

    click.echo(f"Initialized callmem in {callmem_dir}")
    click.echo(f"  Database: {db_path}")
    click.echo(f"  Config:   {config_path}")
    click.echo(f"  Schema:   v{db.get_schema_version()}")


@main.command()
@click.option("--project", "-p", type=click.Path(path_type=Path), default=".")
@click.option("--transport", type=click.Choice(["stdio", "sse"]), default="stdio")
@click.option("--no-workers", is_flag=True, help="Disable background workers.")
def serve(project: Path, transport: str, no_workers: bool) -> None:
    """Start the MCP server."""
    import asyncio

    from callmem.mcp.server import run_stdio

    click.echo(f"Starting callmem MCP server (transport={transport})")
    click.echo(f"Project: {project.resolve()}")
    asyncio.run(run_stdio(project.resolve(), no_workers=no_workers))


@main.command()
@click.option("--project", "-p", type=click.Path(path_type=Path), default=".")
@click.option("--port", type=int, default=None, help="Override UI port.")
@click.option("--host", type=str, default=None, help="Override bind address (default: 0.0.0.0).")
def ui(project: Path, port: int | None, host: str | None) -> None:
    """Start the web UI."""
    from callmem.core.config import load_config
    from callmem.core.database import Database
    from callmem.core.engine import MemoryEngine

    config = load_config(project)
    actual_port = port if port is not None else config.ui.port
    actual_host = host if host is not None else config.ui.host

    db_path = project / ".callmem" / "memory.db"
    db = Database(db_path)
    db.initialize()

    engine = MemoryEngine(db, config)

    import uvicorn

    from callmem.ui.app import create_app

    app = create_app(engine)
    click.echo(f"Starting callmem UI on http://{actual_host}:{actual_port}")
    click.echo(f"Project: {project.resolve()}")
    uvicorn.run(app, host=actual_host, port=actual_port, log_level="info")


def _systemd_unit_state(project: Path) -> str | None:
    """Return 'active'/'inactive'/'failed'/... for callmem-<project>.service, or None."""
    import subprocess

    unit = f"callmem-{project.resolve().name}.service"
    try:
        result = subprocess.run(
            ["systemctl", "--user", "is-active", unit],
            capture_output=True, text=True, timeout=2,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    state = result.stdout.strip()
    # `is-active` returns non-zero for inactive/failed/unknown but still prints the state.
    if not state or state == "unknown":
        # Check if unit exists at all; if not, don't claim "unknown".
        try:
            exists = subprocess.run(
                ["systemctl", "--user", "cat", unit],
                capture_output=True, text=True, timeout=2,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return None
        if exists.returncode != 0:
            return None
    return state or "unknown"


def _probe_ui(host: str, port: int) -> bool:
    """Return True if something on host:port speaks HTTP.

    A plain TCP connect would false-positive any random listener;
    a full GET follows redirects and loads the page. We send a
    GET but treat *any* HTTP response — including 4xx/5xx — as up,
    since those still mean a live HTTP server.
    """
    import http.client

    probe_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    try:
        conn = http.client.HTTPConnection(probe_host, port, timeout=0.5)
        try:
            conn.request("GET", "/")
            conn.getresponse()
            return True
        finally:
            conn.close()
    except (ConnectionError, TimeoutError, OSError):
        return False


def _render_status(project: Path) -> bool:
    """Print status for a single project. Returns False if no DB was found."""
    from callmem.core.config import load_config
    from callmem.core.database import Database

    project = project.resolve()
    db_path = project / ".callmem" / "memory.db"
    if not db_path.exists():
        click.echo(f"No callmem database found at {db_path}")
        click.echo("Run 'callmem init' first.")
        return False

    db = Database(db_path)
    conn = db.connect()
    try:
        version = db.get_schema_version()
        events = conn.execute("SELECT COUNT(*) as c FROM events").fetchone()["c"]
        sessions = conn.execute("SELECT COUNT(*) as c FROM sessions").fetchone()["c"]
        entities = conn.execute("SELECT COUNT(*) as c FROM entities").fetchone()["c"]
        last_session = conn.execute(
            "SELECT started_at FROM sessions ORDER BY started_at DESC LIMIT 1"
        ).fetchone()

        db_size_mb = db_path.stat().st_size / (1024 * 1024)

        click.echo(f"callmem status — {project.resolve()}")
        click.echo(f"  Database:     {db_path} ({db_size_mb:.1f} MB)")
        click.echo(f"  Schema:       v{version}")
        click.echo(f"  Events:       {events}")
        click.echo(f"  Sessions:     {sessions}")
        click.echo(f"  Entities:     {entities}")
        last = last_session["started_at"] if last_session else "none"
        click.echo(f"  Last session: {last}")
    finally:
        conn.close()

    # UI + service info (best-effort; never errors out if config/systemd unavailable).
    try:
        config = load_config(project)
        ui_host, ui_port = config.ui.host, config.ui.port
        reachable = _probe_ui(ui_host, ui_port)
        status_word = "up" if reachable else "down"
        click.echo(f"  UI:           http://{ui_host}:{ui_port}  ({status_word})")
    except Exception:  # noqa: BLE001 — status is diagnostic; config errors shouldn't abort it
        pass

    svc_state = _systemd_unit_state(project)
    if svc_state is not None:
        click.echo(f"  Service:      callmem-{project.name}.service ({svc_state})")

    return True


def _discover_callmem_projects() -> list[Path]:
    """Return project paths from installed callmem-*.service user units."""
    systemd_dir = Path.home() / ".config" / "systemd" / "user"
    if not systemd_dir.is_dir():
        return []
    projects: list[Path] = []
    for unit in sorted(systemd_dir.glob("callmem-*.service")):
        for line in unit.read_text().splitlines():
            if line.startswith("WorkingDirectory="):
                projects.append(Path(line.split("=", 1)[1].strip()))
                break
    return projects


@main.command()
@click.option("--project", "-p", type=click.Path(path_type=Path), default=".")
@click.option(
    "--all",
    "show_all",
    is_flag=True,
    help="Show status for every project with an installed callmem-*.service unit.",
)
def status(project: Path, show_all: bool) -> None:
    """Show memory status for a project (or all projects with --all)."""
    if show_all:
        projects = _discover_callmem_projects()
        if not projects:
            click.echo(
                "No callmem-*.service units found in "
                "~/.config/systemd/user/. Falling back to current project."
            )
            _render_status(project)
            return
        for i, p in enumerate(projects):
            if i > 0:
                click.echo()
            _render_status(p)
        return
    _render_status(project)


@main.command("stats")
@click.option("--project", "-p", type=click.Path(path_type=Path), default=".")
def stats_cmd(project: Path) -> None:
    """Show ingestion and filtering statistics."""
    from callmem.core.config import load_config
    from callmem.core.database import Database

    db_path = project / ".callmem" / "memory.db"
    if not db_path.exists():
        click.echo(f"No callmem database found at {db_path}")
        return

    config = load_config(project)
    db = Database(db_path)
    conn = db.connect()
    try:
        events = conn.execute("SELECT COUNT(*) as c FROM events").fetchone()["c"]
        tool_calls = conn.execute(
            "SELECT COUNT(*) as c FROM events WHERE type = 'tool_call'"
        ).fetchone()["c"]
        pending = conn.execute(
            "SELECT COUNT(*) as c FROM jobs WHERE status = 'pending'"
        ).fetchone()["c"]
        tracked_files = conn.execute(
            "SELECT COUNT(DISTINCT file_path) as c FROM entity_files"
        ).fetchone()["c"]
    finally:
        conn.close()

    skip_tools = config.ingestion.skip_tools
    skip_patterns = config.ingestion.skip_patterns

    click.echo(f"callmem stats — {project.resolve()}")
    click.echo(f"  Events ingested:         {events}")
    click.echo(f"  Events (tool_call):      {tool_calls}")
    click.echo(f"  Pending extraction jobs: {pending}")
    click.echo(f"  Files tracked:           {tracked_files}")
    click.echo()
    click.echo("  Tool filter config:")
    tools_display = ", ".join(skip_tools) if skip_tools else "(none)"
    patterns_display = ", ".join(skip_patterns) if skip_patterns else "(none)"
    click.echo(f"    skip_tools:    {tools_display}")
    click.echo(f"    skip_patterns: {patterns_display}")
    click.echo()
    click.echo(
        "  Note: live skip/file-context counters are in-memory per "
        "running engine. Open the /files page in the web UI to see "
        "the current counts."
    )


@main.command("stale")
@click.option("--project", "-p", type=click.Path(path_type=Path), default=".")
@click.option("--check", "run_check", is_flag=True,
              help="Run a staleness detection pass now.")
@click.option("--reset", "reset_id", default=None,
              help="Unmark an entity ID as stale.")
@click.option("--limit", type=int, default=50)
def stale_cmd(
    project: Path, run_check: bool, reset_id: str | None, limit: int,
) -> None:
    """List, run, or reset entity staleness."""
    from callmem.core.config import load_config
    from callmem.core.database import Database
    from callmem.core.engine import MemoryEngine

    db_path = project / ".callmem" / "memory.db"
    if not db_path.exists():
        click.echo(f"No callmem database found at {db_path}")
        return

    config = load_config(project)
    db = Database(db_path)
    db.initialize()
    engine = MemoryEngine(db, config)

    if reset_id:
        updated = engine.mark_current(reset_id)
        if updated is None:
            click.echo(f"Entity not found: {reset_id}")
            return
        click.echo(
            f"{reset_id}: stale={bool(updated.get('stale', 0))}"
        )
        return

    if run_check:
        from callmem.core.engine import _create_llm_client
        from callmem.core.staleness import StalenessChecker

        llm = _create_llm_client(config)
        if llm is None:
            click.echo("No LLM backend configured — skipping automatic check.")
            click.echo("(Manual mark/unmark via MCP or --reset still works.)")
            return
        checker = StalenessChecker(db, llm)
        decisions = checker.run(engine.project_id)
        click.echo(f"Evaluated {len(decisions)} pair(s).")
        for d in decisions:
            click.echo(
                f"  {d.older_id[:8]} → {d.verdict} "
                f"(by {d.newer_id[:8]}): {d.reason}"
            )
        marked = sum(
            1 for d in decisions
            if d.verdict in ("superseded", "contradicted")
        )
        click.echo(f"Marked {marked} entity(ies) stale.")
        return

    stale = engine.list_stale_entities(limit=limit)
    if not stale:
        click.echo("No stale entities.")
        return
    click.echo(f"{len(stale)} stale entity(ies):")
    for e in stale:
        reason = e.get("staleness_reason") or "?"
        sup = e.get("superseded_by")
        sup_str = f" → {sup[:8]}" if sup else ""
        click.echo(
            f"  {e['id'][:8]} [{e['type']}] "
            f"{(e.get('title') or '')[:60]} "
            f"({reason}{sup_str})"
        )


@main.command()
@click.option("--project", "-p", type=click.Path(path_type=Path), default=".")
@click.option("--interval", type=int, default=5, help="Poll interval in seconds.")
def workers(project: Path, interval: int) -> None:
    """Run the background worker loop standalone."""
    import signal

    from callmem.core.config import load_config
    from callmem.core.database import Database
    from callmem.core.workers import WorkerRunner

    config = load_config(project)
    db_path = project / ".callmem" / "memory.db"
    db = Database(db_path)
    db.initialize()

    from callmem.core.engine import _create_llm_client

    llm_client = _create_llm_client(config)
    if llm_client is None:
        click.echo("LLM backend is 'none' — workers have nothing to process.")
        click.echo("Set [llm] backend = 'ollama' or 'openai_compat' in config.toml.")
        return

    runner = WorkerRunner(
        db, llm_client, config,
        poll_interval=interval,
        project_path=str(project),
    )

    stop_event = threading.Event()

    def _signal_handler(sig: int, frame: object) -> None:
        stop_event.set()


    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    click.echo(f"Starting callmem workers (interval={interval}s)")
    click.echo(f"Project: {project.resolve()}")
    runner.start()

    stop_event.wait()
    runner.stop()
    click.echo("Workers stopped.")


@main.command()
@click.option("--project", "-p", type=click.Path(path_type=Path), default=".")
@click.option(
    "--opencode-url",
    default="http://localhost:4096",
    help="OpenCode server URL.",
)
def adapter(project: Path, opencode_url: str) -> None:
    """Run the OpenCode SSE adapter."""
    from callmem.adapters.opencode import OpenCodeAdapter
    from callmem.core.config import load_config
    from callmem.core.database import Database
    from callmem.core.engine import MemoryEngine

    config = load_config(project)
    db_path = project / ".callmem" / "memory.db"
    db = Database(db_path)
    db.initialize()

    engine = MemoryEngine(db, config)
    oc_adapter = OpenCodeAdapter(engine, opencode_url=opencode_url)

    click.echo(f"Starting OpenCode adapter (url={opencode_url})")
    click.echo(f"Project: {project.resolve()}")

    try:
        oc_adapter.run()
    except KeyboardInterrupt:
        oc_adapter.stop()
        click.echo("Adapter stopped.")


@main.command()
@click.option("--project", "-p", type=click.Path(path_type=Path), default=".")
@click.option("--port", type=int, default=None, help="Override UI port.")
@click.option("--host", type=str, default=None, help="Override bind address.")
@click.option(
    "--opencode-url",
    default="http://localhost:4096",
    help="OpenCode server URL.",
)
@click.option(
    "--no-adapter",
    is_flag=True,
    help="Skip the OpenCode SSE adapter.",
)
@click.option(
    "--no-workers",
    is_flag=True,
    help="Skip background workers.",
)
def daemon(
    project: Path,
    port: int | None,
    host: str | None,
    opencode_url: str,
    no_adapter: bool,
    no_workers: bool,
) -> None:
    """Run UI + workers + adapter in a single process."""
    import signal

    import uvicorn

    from callmem.core.config import load_config
    from callmem.core.database import Database
    from callmem.core.engine import MemoryEngine, _create_llm_client
    from callmem.ui.app import create_app

    config = load_config(project)
    actual_port = port if port is not None else config.ui.port
    actual_host = host if host is not None else config.ui.host

    db_path = project / ".callmem" / "memory.db"
    db = Database(db_path)
    db.initialize()

    engine = MemoryEngine(db, config)
    app = create_app(engine)

    stop_event = threading.Event()
    threads: list[threading.Thread] = []

    # Workers
    worker_runner = None
    if not no_workers:
        llm_client = _create_llm_client(config)
        if llm_client is not None:
            from callmem.core.workers import WorkerRunner

            worker_runner = WorkerRunner(
                db, llm_client, config,
                event_bus=app.state.event_bus,
                project_path=str(project),
            )
            worker_runner.start()
            click.echo("  Workers:  started")
        else:
            click.echo(
                "  Workers:  skipped (backend='none')"
            )
    else:
        click.echo("  Workers:  disabled")

    # Adapters: OpenCode SSE + OpenCode DB + Claude Code JSONL tailer.
    # Each is independently gated by --no-adapter and the [adapters] config.
    adapter_instances: list[Any] = []
    if not no_adapter:
        if config.adapters.opencode:
            from callmem.adapters.opencode import OpenCodeAdapter

            oc = OpenCodeAdapter(engine, opencode_url=opencode_url)
            adapter_instances.append(oc)

            def _run_oc(adapter: OpenCodeAdapter = oc) -> None:
                import contextlib
                with contextlib.suppress(Exception):
                    adapter.run()

            t = threading.Thread(
                target=_run_oc, daemon=True, name="callmem-opencode-adapter",
            )
            t.start()
            threads.append(t)
            click.echo(f"  OpenCode: {opencode_url}")
        else:
            click.echo("  OpenCode: disabled (config)")

        if config.adapters.opencode_db:
            from callmem.adapters.opencode_db import OpenCodeDBAdapter

            ocdb = OpenCodeDBAdapter(
                engine,
                project_path=project.resolve(),
                poll_interval=config.adapters.opencode_db_poll_interval,
                idle_timeout=config.adapters.opencode_db_idle_timeout,
            )
            adapter_instances.append(ocdb)

            def _run_ocdb(adapter: OpenCodeDBAdapter = ocdb) -> None:
                import contextlib
                with contextlib.suppress(Exception):
                    adapter.run()

            t = threading.Thread(
                target=_run_ocdb, daemon=True, name="callmem-opencode-db-adapter",
            )
            t.start()
            threads.append(t)
            click.echo("  OpenCode DB: enabled")
        else:
            click.echo("  OpenCode DB: disabled (config)")

        if config.adapters.claude_code:
            from callmem.adapters.claude_code import ClaudeCodeAdapter

            cc = ClaudeCodeAdapter(
                engine,
                project_path=project.resolve(),
                poll_interval=config.adapters.claude_code_poll_interval,
                idle_timeout=config.adapters.claude_code_idle_timeout,
            )
            adapter_instances.append(cc)

            def _run_cc(adapter: ClaudeCodeAdapter = cc) -> None:
                import contextlib
                with contextlib.suppress(Exception):
                    adapter.run()

            t = threading.Thread(
                target=_run_cc, daemon=True,
                name="callmem-claude-code-adapter",
            )
            t.start()
            threads.append(t)
            click.echo(f"  Claude:   {cc.cc_dir}")
        else:
            click.echo("  Claude:   disabled (config)")
    else:
        click.echo("  Adapters: disabled (--no-adapter)")

    click.echo(
        f"  UI:       http://{actual_host}:{actual_port}"
    )
    click.echo()
    click.echo(
        f"callmem daemon running — {project.resolve()}"
    )
    click.echo("Press Ctrl+C to stop.")

    def _shutdown(sig: int, frame: object) -> None:
        click.echo("\nShutting down...")
        stop_event.set()
        for a in adapter_instances:
            a.stop()
        if worker_runner is not None:
            worker_runner.stop()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # UI runs on the main thread (uvicorn blocks)
    try:
        uvicorn.run(
            app,
            host=actual_host,
            port=actual_port,
            log_level="warning",
        )
    except SystemExit:
        pass
    finally:
        stop_event.set()
        for a in adapter_instances:
            a.stop()
        if worker_runner is not None:
            worker_runner.stop()
        for t in threads:
            t.join(timeout=5)


@main.command("import")
@click.option("--project", "-p", type=click.Path(path_type=Path), default=".")
@click.option(
    "--source",
    type=click.Choice(["opencode", "claude-code"]),
    default=None,
    help="Source to import from. Required unless --status is set.",
)
@click.option("--session-id", default=None, help="Import a specific session ID.")
@click.option(
    "--opencode-db",
    type=click.Path(path_type=Path),
    default=None,
    help="Override path to OpenCode SQLite database.",
)
@click.option(
    "--project-path",
    default=None,
    help="Only import sessions for this project worktree path.",
)
@click.option("--all", "import_all", is_flag=True, help="Import all sessions.")
@click.option("--dry-run", is_flag=True, help="Show what would be imported.")
@click.option("--background", is_flag=True, help="Run import in background process.")
@click.option("--status", "show_status", is_flag=True, help="Show import progress/status.")
def import_cmd(
    project: Path,
    source: str | None,
    session_id: str | None,
    opencode_db: Path | None,
    project_path: str | None,
    import_all: bool,
    dry_run: bool,
    background: bool,
    show_status: bool,
) -> None:
    """Import session history from an external source."""
    if show_status:
        _show_import_status(project)
        return

    if source is None:
        raise click.UsageError(
            "Missing option '--source'. Choose from: opencode, claude-code."
        )

    from callmem.core.config import load_config
    from callmem.core.database import Database
    from callmem.core.engine import MemoryEngine

    config = load_config(project)
    db_path = project / ".callmem" / "memory.db"
    if not db_path.exists():
        click.echo(f"No callmem database found at {db_path}")
        click.echo("Run 'callmem init' first.")
        return

    db = Database(db_path)
    db.initialize()
    engine = MemoryEngine(db, config)

    import time

    start_time = time.monotonic()
    jobs_before = _count_pending_jobs(db_path)

    progress_updates: list[dict] = []

    def _on_progress(update: dict) -> None:
        progress_updates.append(update)
        phase = update.get("phase", "")
        if phase == "discovery":
            est = update.get("total_events_estimate")
            est_str = f" (~{est} events)" if est is not None else ""
            click.echo(
                f"  Discovered {update['total_sessions']} sessions{est_str}"
            )
        elif phase == "importing":
            idx = update["session_index"]
            total = update["total_sessions"]
            title = (update.get("session_title") or "untitled")[:40]
            events = update.get("session_events", 0)
            pct = idx / total if total else 0
            filled = int(20 * pct)
            bar = "\u2588" * filled + "\u2591" * (20 - filled)
            events_so_far = update.get("total_events_so_far", 0)
            skipped = " [skip]" if update.get("skipped") else ""
            click.echo(
                f"  [{bar}] {idx}/{total} sessions "
                f"({events_so_far} events) "
                f"— {title} ({events} events){skipped}"
            )

    if source == "claude-code":
        from callmem.adapters.claude_code_import import (
            import_sessions as cc_import_sessions,
        )

        click.echo(f"Reading Claude Code transcripts for {project.resolve()}...")
        if background and not dry_run:
            _run_background_import(
                project, source, None, session_id, project_path,
            )
            return

        results = cc_import_sessions(
            engine,
            project_path=project.resolve(),
            progress_callback=_on_progress,
            project=project,
            dry_run=dry_run,
        )
    else:
        from callmem.adapters.opencode_import import (
            DEFAULT_DB_PATH,
            import_sessions,
        )

        oc_db = opencode_db if opencode_db is not None else DEFAULT_DB_PATH
        click.echo(f"Reading {source} sessions from {oc_db}...")

        if background and not dry_run:
            _run_background_import(
                project, source, oc_db, session_id, project_path,
            )
            return

        results = import_sessions(
            engine,
            db_path=oc_db,
            session_id=session_id,
            project_path=project_path,
            import_all=import_all,
            dry_run=dry_run,
            progress_callback=_on_progress,
            project=project,
        )

    if not results:
        click.echo("No sessions found.")
        return

    elapsed = time.monotonic() - start_time
    jobs_after = _count_pending_jobs(db_path)
    jobs_queued = max(0, jobs_after - jobs_before)

    for r in results:
        if r.get("dry_run"):
            proj = r.get("project_name", "")
            proj_info = f" [{proj}]" if proj else ""
            click.echo(
                f"  [dry-run] {r['source_id']}: "
                f"{r.get('title', '')} ({r.get('message_count', 0)} messages){proj_info}"
            )
        else:
            errors = r.get("errors", [])
            status_str = "OK" if not errors else f"{len(errors)} errors"
            click.echo(
                f"  Imported {r['source_id']}: "
                f"{r.get('event_count', 0)} events ({status_str})"
            )

    if dry_run or (not import_all and session_id is None):
        click.echo(f"\nFound {len(results)} session(s). Use --all to import all.")
    else:
        imported = len([r for r in results if not r.get("dry_run")])
        total_events = sum(r.get("event_count", 0) for r in results if not r.get("dry_run"))
        elapsed_str = _format_elapsed(elapsed)
        click.echo()
        click.echo("Import complete:")
        click.echo(f"  Sessions: {imported} imported")
        click.echo(f"  Events:   {total_events} ingested")
        click.echo(f"  Jobs:     {jobs_queued} extraction jobs queued")
        click.echo(f"  Time:     {elapsed_str}")
        click.echo()
        click.echo("Extraction will continue in the background via the worker.")

        # Generate SESSION_SUMMARY.md so agents pick up context immediately
        _write_session_summary(project, config, db, engine)


def _show_import_status(project: Path) -> None:
    """Show the current import progress or last import summary."""
    from callmem.adapters.opencode_import import read_import_progress

    progress = read_import_progress(project)
    if not progress:
        click.echo("No import in progress.")
        click.echo("No previous import found.")
        return

    status = progress.get("status", "unknown")
    if status == "running":
        click.echo("Import in progress:")
        click.echo(
            f"  Sessions: {progress.get('imported_sessions', 0)}/"
            f"{progress.get('total_sessions', '?')} imported"
        )
        click.echo(
            f"  Events:   {progress.get('imported_events', 0)}/"
            f"{progress.get('total_events', '?')} ingested"
        )
        click.echo(f"  PID:      {progress.get('pid', '?')}")
    elif status == "completed":
        click.echo("No import in progress.")
        completed = progress.get("completed_at", "unknown")
        click.echo(
            f"Last import: {progress.get('imported_sessions', '?')} sessions, "
            f"{progress.get('imported_events', '?')} events "
            f"(completed {completed})"
        )
    elif status == "stale":
        click.echo("Previous import process is no longer running (stale).")
        click.echo(
            f"  Sessions: {progress.get('imported_sessions', '?')}/"
            f"{progress.get('total_sessions', '?')}"
        )
    else:
        click.echo(f"Import status: {status}")


def _run_background_import(
    project: Path,
    source: str,
    oc_db: Path | None,
    session_id: str | None,
    project_path: str | None,
) -> None:
    """Fork the import into a background subprocess."""
    import subprocess
    import sys

    cmd = [
        sys.executable, "-m", "callmem.cli",
        "import",
        "--source", source,
        "--project", str(project),
        "--all",
    ]
    if source == "opencode" and oc_db is not None:
        cmd.extend(["--opencode-db", str(oc_db)])
    if session_id:
        cmd.extend(["--session-id", session_id])
    if project_path:
        cmd.extend(["--project-path", project_path])

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    click.echo(f"Import running in background (PID {proc.pid}).")
    click.echo("Check progress: callmem import --status")
    click.echo("Extraction will begin automatically once events are ingested.")
    click.echo("You can open OpenCode now — new memories will appear as they're processed.")


def _count_pending_jobs(db_path: Path) -> int:
    """Count pending extraction jobs in the database."""
    import sqlite3

    if not db_path.exists():
        return 0
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        row = conn.execute(
            "SELECT COUNT(*) as c FROM jobs WHERE status = 'pending'"
        ).fetchone()
        conn.close()
        return row[0] if row else 0
    except Exception:
        return 0


def _format_elapsed(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    mins = int(seconds // 60)
    secs = int(seconds % 60)
    return f"{mins}m {secs}s"


def _write_session_summary(
    project: Path, config: object, db: object, engine: object,
) -> None:
    """Generate SESSION_SUMMARY.md in the project root."""
    from callmem.core.briefing import BriefingGenerator

    if not config.briefing.auto_write_session_summary:
        return

    try:
        gen = BriefingGenerator(engine.repo, config, engine.ollama)
        briefing = gen.write_session_summary(
            project_id=engine.project_id,
            project_name=config.project.name or "default",
            worktree_path=project,
        )
        click.echo(f"  Wrote SESSION_SUMMARY.md ({briefing.token_count} tokens)")
    except Exception as exc:
        click.echo(f"  Warning: could not write SESSION_SUMMARY.md: {exc}")


@main.command()
@click.option("--project", "-p", type=click.Path(path_type=Path), default=".")
@click.option("--write", "write_file", is_flag=True, help="Write to SESSION_SUMMARY.md")
def briefing(project: Path, write_file: bool) -> None:
    """Generate and display the startup briefing."""
    from callmem.core.briefing import BriefingGenerator
    from callmem.core.config import load_config
    from callmem.core.database import Database
    from callmem.core.engine import MemoryEngine

    config = load_config(project)
    db_path = project / ".callmem" / "memory.db"
    if not db_path.exists():
        click.echo(f"No callmem database found at {db_path}")
        return

    db = Database(db_path)
    db.initialize()
    engine = MemoryEngine(db, config)
    gen = BriefingGenerator(engine.repo, config, engine.ollama)

    if write_file:
        result = gen.write_session_summary(
            project_id=engine.project_id,
            project_name=config.project.name or "default",
            worktree_path=project,
        )
        click.echo(result.content)
        click.echo(f"\nWritten to {project / 'SESSION_SUMMARY.md'}")
    else:
        result = gen.generate(
            project_id=engine.project_id,
            project_name=config.project.name or "default",
        )
        click.echo(result.content)


@main.command("re-extract")
@click.option("--project", "-p", type=click.Path(path_type=Path), default=".")
@click.option("--session", "session_id", default=None, help="Limit to a specific session ID.")
@click.option("--since", default=None, help="Limit to events from last N days (e.g. 7d).")
@click.option("--batch-size", type=int, default=None, help="Override extraction batch size.")
@click.option("--dry-run", is_flag=True, help="Show scope without executing.")
@click.option("--force", is_flag=True, help="Overwrite all entities including edited ones.")
@click.option(
    "--no-preserve-edits/--preserve-edits",
    default=True,
    help="Skip manually modified entities (default: preserve).",
)
def re_extract(
    project: Path,
    session_id: str | None,
    since: str | None,
    batch_size: int | None,
    dry_run: bool,
    force: bool,
    no_preserve_edits: bool,
) -> None:
    """Re-extract entities from existing events using the current model."""
    import time

    from callmem.core.config import load_config
    from callmem.core.database import Database
    from callmem.core.engine import MemoryEngine
    from callmem.core.reextraction import ReExtractor

    config = load_config(project)
    db_path = project / ".callmem" / "memory.db"
    if not db_path.exists():
        click.echo(f"No callmem database found at {db_path}")
        click.echo("Run 'callmem init' first.")
        return

    db = Database(db_path)
    db.initialize()
    engine = MemoryEngine(db, config)

    if engine.llm_client is None:
        click.echo("LLM backend is 'none' — nothing to re-extract with.")
        return

    if not engine.llm_client.is_available():
        click.echo(f"Ollama not reachable at {config.ollama.endpoint}")
        click.echo("Start Ollama first and try again.")
        return

    re_extractor = ReExtractor(db, engine.llm_client, config)
    project_id = engine.project_id

    total_events = re_extractor.count_events(project_id, session_id, since)
    total_sessions = re_extractor.count_sessions(project_id, session_id, since)

    if total_events == 0:
        click.echo("No events found matching the given filters.")
        return

    ctx_val = config.ollama.num_ctx or "auto"
    click.echo(
        f"Re-extracting with {config.ollama.model} (num_ctx: {ctx_val})"
    )
    click.echo()

    if dry_run:
        result = re_extractor.run(
            project_id, session_id=session_id, since=since,
            batch_size=batch_size, dry_run=True,
        )
        click.echo(f"  Sessions: {result['total_sessions']}")
        click.echo(f"  Events:   {result['total_events']}")
        click.echo(f"  Batches:  {result['batches']}")
        click.echo()
        click.echo("Use without --dry-run to execute.")
        return

    preserve = bool(not force)

    est_minutes = max(1, total_events // 100)
    click.echo(f"Re-extract {total_events} events across {total_sessions} sessions?")
    click.echo(f"Estimated time: ~{est_minutes} minute(s)")
    click.echo("Existing entities will be archived (not deleted).")
    if preserve:
        click.echo("Pinned and edited entities will be preserved.")
    click.echo()

    proceed = click.confirm("Proceed?", default=False)
    if not proceed:
        click.echo("Cancelled.")
        return

    click.echo()
    start_time = time.monotonic()

    def _on_progress(update: dict) -> None:
        batch = update["batch"]
        total = update["total_batches"]
        evts = update["events_processed"]
        total_evts = update["total_events"]
        ents = update["entities_created"]
        click.echo(
            f"  Batch {batch}/{total} — {evts}/{total_evts} events, "
            f"{ents} entities created"
        )

    result = re_extractor.run(
        project_id,
        session_id=session_id,
        since=since,
        batch_size=batch_size,
        force=force,
        dry_run=False,
        progress_callback=_on_progress,
    )

    elapsed = time.monotonic() - start_time
    if elapsed < 60:
        elapsed_str = f"{elapsed:.0f}s"
    else:
        elapsed_str = f"{int(elapsed // 60)}m {int(elapsed % 60)}s"

    click.echo()
    click.echo("Re-extraction complete:")
    click.echo(f"  Events processed:  {result['events_processed']}")
    click.echo(f"  Entities created:  {result['entities_created']}")
    click.echo(f"  Entities archived: {result['entities_archived']}")
    click.echo(f"  Time:              {elapsed_str}")


# ── Watch command ────────────────────────────────────────────────────


@main.command()
@click.option("--project", "-p", type=click.Path(path_type=Path), default=".")
@click.option("--interval", "-n", type=int, default=30, help="Refresh interval in seconds.")
@click.option("--once", is_flag=True, help="Show once and exit.")
def watch(project: Path, interval: int, once: bool) -> None:
    """Watch job queue progress with live ETA."""
    import sqlite3
    import time
    from datetime import datetime

    db_path = project / ".callmem" / "memory.db"
    if not db_path.exists():
        click.echo(f"No callmem database found at {db_path}", err=True)
        raise SystemExit(1)

    def _show() -> None:
        db = sqlite3.connect(str(db_path))
        counts = dict(db.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status").fetchall())
        pending = counts.get("pending", 0)
        completed = counts.get("completed", 0)
        running = counts.get("running", 0)
        failed = counts.get("failed", 0)
        total = pending + completed + running + failed

        recent = db.execute(
            "SELECT MIN(completed_at), MAX(completed_at), COUNT(*) "
            "FROM jobs WHERE status='completed' "
            "AND completed_at > datetime('now', '-10 minutes')"
        ).fetchone()

        rate = ""
        eta = ""
        if recent and recent[2] > 1 and recent[0] and recent[1]:
            t0 = datetime.fromisoformat(recent[0])
            t1 = datetime.fromisoformat(recent[1])
            secs = (t1 - t0).total_seconds()
            if secs > 0:
                per_min = recent[2] / (secs / 60)
                mins_left = pending / per_min if per_min > 0 else 0
                rate = f"{per_min:.1f} jobs/min"
                eta = (
                    f"{mins_left / 60:.1f} hours"
                    if mins_left > 60
                    else f"{mins_left:.0f} min"
                )

        pct = int(completed / total * 100) if total > 0 else 0
        bar_len = 30
        filled = int(bar_len * completed / total) if total > 0 else 0
        bar = "\u2588" * filled + "\u2591" * (bar_len - filled)

        click.echo(f"[{bar}] {pct}%")
        click.echo(f"Completed: {completed}/{total}")
        click.echo(f"Pending:   {pending}")
        click.echo(f"Running:   {running}")
        if failed:
            click.echo(f"Failed:    {failed}")
        if rate:
            click.echo(f"Rate:      {rate}")
        if eta:
            click.echo(f"ETA:       {eta}")
        db.close()

    if once:
        _show()
        return

    try:
        while True:
            click.echo("\033[2J\033[H", nl=False)
            click.echo(f"callmem job queue \u2014 {datetime.now().strftime('%H:%M:%S')}")
            click.echo()
            _show()
            time.sleep(interval)
    except KeyboardInterrupt:
        click.echo()


# ── Corpus commands ──────────────────────────────────────────────────


@main.group()
def corpus() -> None:
    """Manage knowledge corpora."""


@corpus.command("build")
@click.argument("name")
@click.option("--types", "-t", help="Comma-separated entity types")
@click.option("--since", help="Start date YYYY-MM-DD")
@click.option("--until", help="End date YYYY-MM-DD")
@click.option("--query", "-q", help="Search filter")
@click.option("--project", "-p", type=click.Path(path_type=Path), default=".")
def corpus_build(
    name: str,
    types: str | None,
    since: str | None,
    until: str | None,
    query: str | None,
    project: Path,
) -> None:
    """Build a corpus from filtered entities."""
    from callmem.core.knowledge import KnowledgeAgent
    from callmem.core.ollama import OllamaClient

    db, config = _get_db_and_config(project)
    ollama = OllamaClient()
    agent = KnowledgeAgent(db, ollama)
    project_id = _resolve_project_id(db, config)

    type_list = types.split(",") if types else None
    result = agent.build_corpus(
        name=name,
        project_id=project_id,
        types=type_list,
        date_start=since,
        date_end=until,
        query=query,
    )
    click.echo(
        f"Built corpus '{name}': "
        f"{result['entity_count']} entities, "
        f"{result['token_count']} tokens"
    )
    if result.get("warning"):
        click.echo(f"  Warning: {result['warning']}")


@corpus.command("list")
@click.option("--project", "-p", type=click.Path(path_type=Path), default=".")
def corpus_list(project: Path) -> None:
    """List all corpora."""
    from callmem.core.knowledge import KnowledgeAgent
    from callmem.core.ollama import OllamaClient

    db, config = _get_db_and_config(project)
    ollama = OllamaClient()
    agent = KnowledgeAgent(db, ollama)
    corpora = agent.list_corpora()
    if not corpora:
        click.echo("No corpora found.")
        return
    for c in corpora:
        click.echo(
            f"  {c['name']}: {c['entity_count']} entities, "
            f"{c['token_count']} tokens "
            f"(updated {c['updated_at'][:10]})"
        )


@corpus.command("query")
@click.argument("name")
@click.argument("question")
@click.option("--project", "-p", type=click.Path(path_type=Path), default=".")
def corpus_query(name: str, question: str, project: Path) -> None:
    """Ask a question against a corpus."""
    from callmem.core.knowledge import KnowledgeAgent
    from callmem.core.ollama import OllamaClient

    db, config = _get_db_and_config(project)
    ollama = OllamaClient()
    agent = KnowledgeAgent(db, ollama)
    answer = agent.query_corpus(name, question)
    click.echo(answer)


@corpus.command("rebuild")
@click.argument("name")
@click.option("--project", "-p", type=click.Path(path_type=Path), default=".")
def corpus_rebuild(name: str, project: Path) -> None:
    """Rebuild a corpus with latest entities."""
    from callmem.core.knowledge import KnowledgeAgent
    from callmem.core.ollama import OllamaClient

    db, config = _get_db_and_config(project)
    ollama = OllamaClient()
    agent = KnowledgeAgent(db, ollama)
    result = agent.rebuild_corpus(name)
    click.echo(
        f"Rebuilt corpus '{name}': "
        f"{result['entity_count']} entities, "
        f"{result['token_count']} tokens"
    )


@corpus.command("delete")
@click.argument("name")
@click.option("--project", "-p", type=click.Path(path_type=Path), default=".")
def corpus_delete(name: str, project: Path) -> None:
    """Delete a corpus."""
    from callmem.core.knowledge import KnowledgeAgent
    from callmem.core.ollama import OllamaClient

    db, config = _get_db_and_config(project)
    ollama = OllamaClient()
    agent = KnowledgeAgent(db, ollama)
    agent.delete_corpus(name)
    click.echo(f"Deleted corpus '{name}'.")


def _get_db_and_config(project: Path) -> tuple:
    from callmem.core.database import Database
    from callmem.models.config import Config

    db_path = project / ".callmem" / "memory.db"
    if not db_path.exists():
        click.echo(f"No callmem database found at {db_path}", err=True)
        raise SystemExit(1)
    db = Database(db_path)
    db.initialize()
    config_path = project / ".callmem" / "config.toml"
    config = Config()
    if config_path.exists():
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib
        data = tomllib.loads(config_path.read_text())
        config = Config.from_dict(data)
    return db, config


def _resolve_project_id(db, config):
    project_name = config.project.name or "default"
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT id FROM projects WHERE name = ?", (project_name,)
        ).fetchone()
        if row:
            return row["id"]
    finally:
        conn.close()
    return None


# ── Resolve command (sweep stale TODOs closed by prior features) ─────


@main.command()
@click.option("--project", "-p", type=click.Path(path_type=Path), default=".")
@click.option(
    "--dry-run", is_flag=True,
    help="Report what would be closed without updating the database.",
)
def resolve(project: Path, dry_run: bool) -> None:
    """Retroactively close open TODOs/failures matched by prior drivers.

    The extraction loop auto-closes TODOs when a resolving feature is
    extracted afterwards, but mis-ordered pairs (TODO extracted *after*
    its driver, or extraction skipped the match) accumulate over time.
    This sweep walks every non-stale feature/bugfix/change in the project
    against the current open pool and applies the same keyword matcher.
    """
    from callmem.core.database import Database
    from callmem.core.extraction import EntityExtractor

    db_path = project / ".callmem" / "memory.db"
    if not db_path.exists():
        click.echo(f"No callmem database found at {db_path}", err=True)
        raise SystemExit(1)

    db = Database(db_path)
    db.initialize()

    from callmem.core.config import load_config

    config = load_config(project)
    project_id = _resolve_project_id(db, config)
    if project_id is None:
        click.echo(f"No project named {config.project.name!r} found.", err=True)
        raise SystemExit(1)

    extractor = EntityExtractor(db, ollama=None)  # type: ignore[arg-type]
    records = extractor.sweep_resolutions(project_id, dry_run=dry_run)

    if not records:
        click.echo("Nothing to resolve — all TODOs/failures already closed.")
        return

    prefix = "Would close" if dry_run else "Closed"
    click.echo(f"{prefix} {len(records)} item(s):")
    for r in records:
        short = r["id"][-8:]
        arrow = f"→ {r['status']}"
        click.echo(
            f"  #{short} [{r['type']}] {r['title'][:60]} {arrow}"
        )
        click.echo(f"      matched by: {r['driver_title'][:70]}")


# ── Audit command (database integrity checks) ────────────────────────


@main.command()
@click.option("--project", "-p", type=click.Path(path_type=Path), default=".")
def audit(project: Path) -> None:
    """Report database integrity issues without making any changes.

    Checks the invariants that the earlier llm-mem → callmem
    contamination incidents violated: entity/event project_id
    consistency, orphaned foreign keys, and per-project counts.
    Exits non-zero if any issue is found so CI can gate on a clean
    report.
    """
    from callmem.core.database import Database

    db_path = project / ".callmem" / "memory.db"
    if not db_path.exists():
        click.echo(f"No callmem database found at {db_path}", err=True)
        raise SystemExit(1)

    db = Database(db_path)
    db.initialize()
    conn = db.connect()
    try:
        cross_project = conn.execute(
            "SELECT COUNT(*) c FROM entities e "
            "JOIN events ev ON ev.id = e.source_event_id "
            "WHERE e.project_id != ev.project_id",
        ).fetchone()["c"]

        dangling_event_refs = conn.execute(
            "SELECT COUNT(*) c FROM entities e "
            "WHERE e.source_event_id IS NOT NULL "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM events ev WHERE ev.id = e.source_event_id"
            ")",
        ).fetchone()["c"]

        dangling_session_refs = conn.execute(
            "SELECT COUNT(*) c FROM events ev "
            "WHERE ev.session_id IS NOT NULL "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM sessions s WHERE s.id = ev.session_id"
            ")",
        ).fetchone()["c"]

        orphaned_sessions = conn.execute(
            "SELECT COUNT(*) c FROM sessions s "
            "WHERE NOT EXISTS ("
            "  SELECT 1 FROM events ev WHERE ev.session_id = s.id"
            ") AND s.status = 'ended'",
        ).fetchone()["c"]

        per_project = conn.execute(
            "SELECT p.name, "
            "  (SELECT COUNT(*) FROM events ev WHERE ev.project_id = p.id) events, "
            "  (SELECT COUNT(*) FROM entities e WHERE e.project_id = p.id) entities, "
            "  (SELECT COUNT(*) FROM sessions s WHERE s.project_id = p.id) sessions "
            "FROM projects p ORDER BY p.name",
        ).fetchall()
    finally:
        conn.close()

    issues = cross_project + dangling_event_refs + dangling_session_refs

    click.echo(f"callmem audit — {db_path}")
    click.echo()
    click.echo("Integrity:")
    click.echo(
        f"  Cross-project entity/event mismatches: {cross_project}",
    )
    click.echo(
        f"  Entities with dangling source_event_id: {dangling_event_refs}",
    )
    click.echo(
        f"  Events with dangling session_id:        {dangling_session_refs}",
    )
    click.echo(
        f"  Ended sessions with no events:          {orphaned_sessions}  "
        f"(informational)",
    )
    click.echo()
    click.echo("Per-project counts:")
    for r in per_project:
        click.echo(
            f"  {r['name']}: events={r['events']} "
            f"entities={r['entities']} sessions={r['sessions']}",
        )

    if issues:
        click.echo()
        click.echo(
            f"FAIL: {issues} integrity issue(s) found.",
            err=True,
        )
        raise SystemExit(2)
    click.echo()
    click.echo("OK: no integrity issues.")


# ── Vacuum command (SQLite space reclamation) ────────────────────────


@main.command()
@click.option("--project", "-p", type=click.Path(path_type=Path), default=".")
def vacuum(project: Path) -> None:
    """Reclaim free pages in the SQLite database via ``VACUUM``.

    Heavy cleanups — stale-entity archival, re-extraction, large event
    purges — leave free pages inside ``memory.db`` that SQLite won't
    return to the filesystem until a ``VACUUM`` rewrites the file.
    Run this after any bulk deletion if disk footprint matters.
    """
    from callmem.core.database import Database

    db_path = project / ".callmem" / "memory.db"
    if not db_path.exists():
        click.echo(f"No callmem database found at {db_path}", err=True)
        raise SystemExit(1)

    size_before = db_path.stat().st_size
    db = Database(db_path)
    conn = db.connect()
    try:
        conn.isolation_level = None  # VACUUM cannot run inside a transaction
        conn.execute("VACUUM")
    finally:
        conn.close()

    size_after = db_path.stat().st_size
    reclaimed = size_before - size_after
    click.echo(
        f"VACUUM complete: "
        f"{_format_bytes(size_before)} → {_format_bytes(size_after)} "
        f"(reclaimed {_format_bytes(reclaimed)})"
    )


def _format_bytes(n: int) -> str:
    """Format a byte count with human-readable units."""
    if n < 0:
        return f"-{_format_bytes(-n)}"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


# ── Migrate command (llm-mem → callmem) ──────────────────────────────


@main.command()
@click.option(
    "--path",
    "-p",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
    help="Project path (default: current directory).",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Show what would change without making changes.",
)
def migrate(path: Path, dry_run: bool) -> None:
    """Migrate a project from legacy ``llm-mem`` names to ``callmem``.

    Renames ``.llm-mem/`` → ``.callmem/`` (if the new dir doesn't already exist)
    and rewrites ``.mcp.json`` / ``opencode.json`` to point at the new package.
    Safe to run multiple times; idempotent.
    """
    import json

    project = path.resolve()
    changes: list[str] = []

    # 1. Rename .llm-mem/ → .callmem/
    old_dir = project / ".llm-mem"
    new_dir = project / ".callmem"
    if old_dir.is_dir():
        if new_dir.exists():
            click.echo(
                f"  skip: both {old_dir.name}/ and {new_dir.name}/ exist — "
                "merge manually."
            )
        else:
            changes.append(f"rename  {old_dir.name}/  →  {new_dir.name}/")
            if not dry_run:
                old_dir.rename(new_dir)

    # 2. Update .mcp.json and opencode.json
    for config_name in (".mcp.json", "opencode.json"):
        cfg_path = project / config_name
        if not cfg_path.exists():
            continue
        try:
            data = json.loads(cfg_path.read_text())
        except json.JSONDecodeError as exc:
            click.echo(f"  skip: {config_name} is invalid JSON: {exc}", err=True)
            continue

        # The MCP server map lives at different keys in different formats:
        #   .mcp.json      → "mcpServers"
        #   opencode.json  → "mcp"
        modified = False
        for servers_key in ("mcpServers", "mcp"):
            servers = data.get(servers_key)
            if not isinstance(servers, dict):
                continue
            if "llm-mem" in servers:
                entry = servers.pop("llm-mem")
                # Preserve insertion order by re-inserting under the new key
                new_servers = {"callmem": entry, **servers}
                data[servers_key] = new_servers
                servers = new_servers
                changes.append(
                    f"{config_name} [{servers_key}]: llm-mem → callmem"
                )
                modified = True
            for name, entry in servers.items():
                if not isinstance(entry, dict):
                    continue
                # .mcp.json uses "args"; opencode.json uses "command" list
                for list_key in ("args", "command"):
                    items = entry.get(list_key)
                    if not isinstance(items, list):
                        continue
                    for i, item in enumerate(items):
                        if item == "llm_mem.mcp.server":
                            items[i] = "callmem.mcp.server"
                            changes.append(
                                f"{config_name} [{name}.{list_key}]: "
                                "llm_mem.mcp.server → callmem.mcp.server"
                            )
                            modified = True

        if modified and not dry_run:
            cfg_path.write_text(json.dumps(data, indent=2) + "\n")

    # 3. Backfill integration template files (idempotent, detection-gated).
    #    Templates ship with the repo; syncs may add files or update stale ones.
    from callmem.core.integrations import (
        ensure_claude_code_commands,
        ensure_opencode_plugin,
    )
    opencode_detected = (
        (project / "opencode.json").exists()
        or (project / ".opencode.json").exists()
    )
    claude_detected = (project / ".mcp.json").exists()
    _silent = lambda m: None  # noqa: E731
    if opencode_detected:
        for name in ensure_opencode_plugin(
            project, echo=_silent, dry_run=dry_run,
        ):
            changes.append(f"sync template: .opencode/.../{name}")
    if claude_detected:
        for name in ensure_claude_code_commands(
            project, echo=_silent, dry_run=dry_run,
        ):
            changes.append(f"sync template: .claude/commands/{name}")

    # Report
    if not changes:
        click.echo("Nothing to migrate — this project is already on callmem.")
        return
    label = "Would make" if dry_run else "Made"
    click.echo(f"{label} {len(changes)} change(s):")
    for c in changes:
        click.echo(f"  • {c}")
    if dry_run:
        click.echo("\nRe-run without --dry-run to apply.")


if __name__ == "__main__":
    main()
