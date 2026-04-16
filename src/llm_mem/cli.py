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
    from llm_mem.core.ollama import OllamaClient
    from llm_mem.core.workers import WorkerRunner

    config = load_config(project)
    db_path = project / ".llm-mem" / "memory.db"
    db = Database(db_path)
    db.initialize()

    ollama = OllamaClient(
        endpoint=config.ollama.endpoint,
        model=config.ollama.model,
        timeout=config.ollama.timeout,
    )

    runner = WorkerRunner(db, ollama, config, poll_interval=interval)

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


if __name__ == "__main__":
    main()
