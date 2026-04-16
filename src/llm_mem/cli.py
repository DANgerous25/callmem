"""CLI entry point for llm-mem."""

from __future__ import annotations

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
    from llm_mem.core.database import Database

    llm_mem_dir = project / ".llm-mem"
    llm_mem_dir.mkdir(exist_ok=True)

    # Initialize database
    db_path = llm_mem_dir / "memory.db"
    db = Database(db_path)
    db.initialize()

    # Write default config
    config_path = llm_mem_dir / "config.toml"
    if not config_path.exists():
        config_path.write_text(_default_config(project.name))

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
    click.echo(f"Starting llm-mem MCP server (transport={transport}, workers={'off' if no_workers else 'on'})")
    click.echo(f"Project: {project.resolve()}")
    # TODO: Launch MCP server (WO-05)
    click.echo("MCP server ready.")


@main.command()
@click.option("--project", "-p", type=click.Path(path_type=Path), default=".")
@click.option("--port", type=int, default=9090)
def ui(project: Path, port: int) -> None:
    """Start the local web UI."""
    click.echo(f"Starting llm-mem UI on http://127.0.0.1:{port}")
    click.echo(f"Project: {project.resolve()}")
    # TODO: Launch FastAPI (WO-10)


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
        click.echo(f"  Database:    {db_path} ({db_size_mb:.1f} MB)")
        click.echo(f"  Schema:      v{version}")
        click.echo(f"  Events:      {events}")
        click.echo(f"  Sessions:    {sessions}")
        click.echo(f"  Entities:    {entities}")
        click.echo(f"  Last session: {last_session['started_at'] if last_session else 'none'}")
    finally:
        conn.close()


def _default_config(project_name: str) -> str:
    return f'''# llm-mem configuration
# See docs/config.md for all options

[project]
name = "{project_name}"

[ollama]
model = "qwen3:8b"
endpoint = "http://localhost:11434"

[briefing]
max_tokens = 2000

[compaction]
enabled = true
schedule = "on_session_end"
'''


if __name__ == "__main__":
    main()
