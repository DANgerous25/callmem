"""CLI entry point for llm-mem."""

from __future__ import annotations

import threading
from pathlib import Path

import click

from llm_mem import __version__


@click.group()
@click.version_option(version=__version__, prog_name="llm-mem")
def main() -> None:
    """llm-mem: Persistent memory for coding agents."""


_SESSION_SUMMARY_SNIPPET = (
    "\n## Startup briefing\n\n"
    "At the **start of every session**, read `SESSION_SUMMARY.md` (in the project root) "
    "if it exists. It contains an auto-generated briefing with recent context, key "
    "entities, and open tasks from previous sessions.\n\n"
    "After reading it, present a brief summary to the user: greet them, state the "
    "project name, mention the most recent session activity, highlight any open TODOs "
    "or unresolved failures, and ask what they'd like to work on.\n"
)


def _ensure_agents_session_summary(agents_path: Path) -> None:
    """Patch an existing AGENTS.md to reference SESSION_SUMMARY.md if missing."""
    if not agents_path.exists():
        return
    content = agents_path.read_text(encoding="utf-8")
    if "SESSION_SUMMARY.md" in content:
        return
    content += _SESSION_SUMMARY_SNIPPET
    agents_path.write_text(content, encoding="utf-8")


def _ensure_opencode_plugin(project: Path) -> None:
    """Install OpenCode auto-briefing plugin and /briefing command if missing or outdated."""
    import filecmp

    templates_dir = Path(__file__).parent.parent.parent / "templates" / "opencode"

    targets = [
        (templates_dir / "plugins" / "auto-briefing.js", project / ".opencode" / "plugins" / "auto-briefing.js"),
        (templates_dir / "commands" / "briefing.md", project / ".opencode" / "commands" / "briefing.md"),
    ]

    for src, dst in targets:
        if not src.exists():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        if not dst.exists() or not filecmp.cmp(src, dst, shallow=False):
            dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")


def _ensure_opencode_instructions(project: Path) -> None:
    """Ensure opencode.json has SESSION_SUMMARY.md in its instructions array."""
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

    instructions = oc_config.get("instructions", [])
    if "SESSION_SUMMARY.md" in instructions:
        return

    instructions.append("SESSION_SUMMARY.md")
    oc_config["instructions"] = instructions
    oc_path.write_text(json.dumps(oc_config, indent=2) + "\n", encoding="utf-8")


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
    """Initialize llm-mem for a project."""
    from llm_mem.core.config import generate_default_config
    from llm_mem.core.database import Database

    llm_mem_dir = project / ".llm-mem"
    llm_mem_dir.mkdir(exist_ok=True)

    db_path = llm_mem_dir / "memory.db"
    db = Database(db_path)
    db.initialize()

    config_path = llm_mem_dir / "config.toml"
    if not config_path.exists():
        config_path.write_text(generate_default_config(project.name))

    agents_template = Path(__file__).parent.parent.parent / "templates" / "AGENTS.md.template"
    agents_path = project / "AGENTS.md"
    if agents_template.exists() and not agents_path.exists():
        agents_path.write_text(agents_template.read_text())

    _ensure_agents_session_summary(agents_path)
    _ensure_opencode_instructions(project)
    _ensure_opencode_plugin(project)

    click.echo(f"Initialized llm-mem in {llm_mem_dir}")
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

    from llm_mem.mcp.server import run_stdio

    click.echo(f"Starting llm-mem MCP server (transport={transport})")
    click.echo(f"Project: {project.resolve()}")
    asyncio.run(run_stdio(project.resolve(), no_workers=no_workers))


@main.command()
@click.option("--project", "-p", type=click.Path(path_type=Path), default=".")
@click.option("--port", type=int, default=None, help="Override UI port.")
@click.option("--host", type=str, default=None, help="Override bind address (default: 0.0.0.0).")
def ui(project: Path, port: int | None, host: str | None) -> None:
    """Start the web UI."""
    from llm_mem.core.config import load_config
    from llm_mem.core.database import Database
    from llm_mem.core.engine import MemoryEngine

    config = load_config(project)
    actual_port = port if port is not None else config.ui.port
    actual_host = host if host is not None else config.ui.host

    db_path = project / ".llm-mem" / "memory.db"
    db = Database(db_path)
    db.initialize()

    engine = MemoryEngine(db, config)

    import uvicorn

    from llm_mem.ui.app import create_app

    app = create_app(engine)
    click.echo(f"Starting llm-mem UI on http://{actual_host}:{actual_port}")
    click.echo(f"Project: {project.resolve()}")
    uvicorn.run(app, host=actual_host, port=actual_port, log_level="info")


@main.command()
@click.option("--project", "-p", type=click.Path(path_type=Path), default=".")
def status(project: Path) -> None:
    """Show memory status for a project."""
    from llm_mem.core.database import Database

    db_path = project / ".llm-mem" / "memory.db"
    if not db_path.exists():
        click.echo(f"No llm-mem database found at {db_path}")
        click.echo("Run 'llm-mem init' first.")
        return

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

        click.echo(f"llm-mem status — {project.resolve()}")
        click.echo(f"  Database:     {db_path} ({db_size_mb:.1f} MB)")
        click.echo(f"  Schema:       v{version}")
        click.echo(f"  Events:       {events}")
        click.echo(f"  Sessions:     {sessions}")
        click.echo(f"  Entities:     {entities}")
        last = last_session["started_at"] if last_session else "none"
        click.echo(f"  Last session: {last}")
    finally:
        conn.close()


@main.command()
@click.option("--project", "-p", type=click.Path(path_type=Path), default=".")
@click.option("--interval", type=int, default=5, help="Poll interval in seconds.")
def workers(project: Path, interval: int) -> None:
    """Run the background worker loop standalone."""
    import signal

    from llm_mem.core.config import load_config
    from llm_mem.core.database import Database
    from llm_mem.core.workers import WorkerRunner

    config = load_config(project)
    db_path = project / ".llm-mem" / "memory.db"
    db = Database(db_path)
    db.initialize()

    from llm_mem.core.engine import _create_llm_client

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

    click.echo(f"Starting llm-mem workers (interval={interval}s)")
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
    from llm_mem.adapters.opencode import OpenCodeAdapter
    from llm_mem.core.config import load_config
    from llm_mem.core.database import Database
    from llm_mem.core.engine import MemoryEngine

    config = load_config(project)
    db_path = project / ".llm-mem" / "memory.db"
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

    from llm_mem.core.config import load_config
    from llm_mem.core.database import Database
    from llm_mem.core.engine import MemoryEngine, _create_llm_client
    from llm_mem.ui.app import create_app

    config = load_config(project)
    actual_port = port if port is not None else config.ui.port
    actual_host = host if host is not None else config.ui.host

    db_path = project / ".llm-mem" / "memory.db"
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
            from llm_mem.core.workers import WorkerRunner

            worker_runner = WorkerRunner(
                db, llm_client, config,
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

    # Adapter
    adapter_instance = None
    if not no_adapter:
        from llm_mem.adapters.opencode import OpenCodeAdapter

        adapter_instance = OpenCodeAdapter(
            engine, opencode_url=opencode_url
        )

        def _run_adapter() -> None:
            try:
                assert adapter_instance is not None
                adapter_instance.run()
            except Exception:
                pass

        t = threading.Thread(
            target=_run_adapter, daemon=True, name="llm-mem-adapter"
        )
        t.start()
        threads.append(t)
        click.echo(f"  Adapter:  {opencode_url}")
    else:
        click.echo("  Adapter:  disabled")

    click.echo(
        f"  UI:       http://{actual_host}:{actual_port}"
    )
    click.echo()
    click.echo(
        f"llm-mem daemon running — {project.resolve()}"
    )
    click.echo("Press Ctrl+C to stop.")

    def _shutdown(sig: int, frame: object) -> None:
        click.echo("\nShutting down...")
        stop_event.set()
        if adapter_instance is not None:
            adapter_instance.stop()
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
        if adapter_instance is not None:
            adapter_instance.stop()
        if worker_runner is not None:
            worker_runner.stop()
        for t in threads:
            t.join(timeout=5)


@main.command("import")
@click.option("--project", "-p", type=click.Path(path_type=Path), default=".")
@click.option(
    "--source",
    type=click.Choice(["opencode"]),
    required=True,
    help="Source to import from.",
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
    source: str,
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

    from llm_mem.adapters.opencode_import import (
        DEFAULT_DB_PATH,
        import_sessions,
    )
    from llm_mem.core.config import load_config
    from llm_mem.core.database import Database
    from llm_mem.core.engine import MemoryEngine

    config = load_config(project)
    db_path = project / ".llm-mem" / "memory.db"
    if not db_path.exists():
        click.echo(f"No llm-mem database found at {db_path}")
        click.echo("Run 'llm-mem init' first.")
        return

    oc_db = opencode_db if opencode_db is not None else DEFAULT_DB_PATH
    click.echo(f"Reading {source} sessions from {oc_db}...")

    if background and not dry_run:
        _run_background_import(
            project, source, oc_db, session_id, project_path,
        )
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
            click.echo(
                f"  Discovered {update['total_sessions']} sessions "
                f"(~{update['total_events_estimate']} events)"
            )
        elif phase == "importing":
            idx = update["session_index"]
            total = update["total_sessions"]
            title = (update.get("session_title") or "untitled")[:40]
            events = update.get("session_events", 0)
            pct = idx / total if total else 0
            filled = int(20 * pct)
            bar = "\u2588" * filled + "\u2591" * (20 - filled)
            click.echo(
                f"  [{bar}] {idx}/{total} sessions "
                f"({update['total_events_so_far']} events) "
                f"— {title} ({events} events)"
            )

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
    from llm_mem.adapters.opencode_import import read_import_progress

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
    oc_db: Path,
    session_id: str | None,
    project_path: str | None,
) -> None:
    """Fork the import into a background subprocess."""
    import subprocess
    import sys

    cmd = [
        sys.executable, "-m", "llm_mem.cli",
        "import",
        "--source", source,
        "--project", str(project),
        "--opencode-db", str(oc_db),
        "--all",
    ]
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
    click.echo("Check progress: llm-mem import --status")
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
    from llm_mem.core.briefing import BriefingGenerator

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
    from llm_mem.core.briefing import BriefingGenerator
    from llm_mem.core.config import load_config
    from llm_mem.core.database import Database
    from llm_mem.core.engine import MemoryEngine

    config = load_config(project)
    db_path = project / ".llm-mem" / "memory.db"
    if not db_path.exists():
        click.echo(f"No llm-mem database found at {db_path}")
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
    from llm_mem.core.knowledge import KnowledgeAgent
    from llm_mem.core.ollama import OllamaClient

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
    from llm_mem.core.knowledge import KnowledgeAgent
    from llm_mem.core.ollama import OllamaClient

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
    from llm_mem.core.knowledge import KnowledgeAgent
    from llm_mem.core.ollama import OllamaClient

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
    from llm_mem.core.knowledge import KnowledgeAgent
    from llm_mem.core.ollama import OllamaClient

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
    from llm_mem.core.knowledge import KnowledgeAgent
    from llm_mem.core.ollama import OllamaClient

    db, config = _get_db_and_config(project)
    ollama = OllamaClient()
    agent = KnowledgeAgent(db, ollama)
    agent.delete_corpus(name)
    click.echo(f"Deleted corpus '{name}'.")


def _get_db_and_config(project: Path) -> tuple:
    from llm_mem.core.database import Database
    from llm_mem.models.config import Config

    db_path = project / ".llm-mem" / "memory.db"
    if not db_path.exists():
        click.echo(f"No llm-mem database found at {db_path}", err=True)
        raise SystemExit(1)
    db = Database(db_path)
    db.initialize()
    config_path = project / ".llm-mem" / "config.toml"
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


if __name__ == "__main__":
    main()
