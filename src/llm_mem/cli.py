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

    runner = WorkerRunner(db, llm_client, config, poll_interval=interval)

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

            worker_runner = WorkerRunner(db, llm_client, config)
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
def import_cmd(
    project: Path,
    source: str,
    session_id: str | None,
    opencode_db: Path | None,
    project_path: str | None,
    import_all: bool,
    dry_run: bool,
) -> None:
    """Import session history from an external source."""
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

    db = Database(db_path)
    db.initialize()
    engine = MemoryEngine(db, config)

    oc_db = opencode_db if opencode_db is not None else DEFAULT_DB_PATH
    click.echo(f"Reading {source} sessions from {oc_db}...")

    results = import_sessions(
        engine,
        db_path=oc_db,
        session_id=session_id,
        project_path=project_path,
        import_all=import_all,
        dry_run=dry_run,
    )

    if not results:
        click.echo("No sessions found.")
        return

    imported = 0
    total_events = 0
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
            status = "OK" if not errors else f"{len(errors)} errors"
            click.echo(
                f"  Imported {r['source_id']}: "
                f"{r.get('event_count', 0)} events ({status})"
            )
            imported += 1
            total_events += r.get("event_count", 0)

    if dry_run or (not import_all and session_id is None):
        click.echo(f"\nFound {len(results)} session(s). Use --all to import all.")
    else:
        click.echo(f"\nImported {imported} session(s), {total_events} events total.")


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
