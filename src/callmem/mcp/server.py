"""MCP server entry point.

Launches callmem as an MCP server over stdio (default) or SSE transport.
Usage:
    python -m callmem.mcp.server --project /path/to/project
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Any

from callmem.core.config import load_config
from callmem.core.database import Database
from callmem.core.engine import MemoryEngine
from callmem.mcp.tools import register_tools

logger = logging.getLogger(__name__)


def _worker_skip_reason(project_path: Path, llm_client: Any) -> str | None:
    """Explain why this MCP server should leave the job queue alone, if so.

    A worker with no API key fails every job it claims, and a second worker
    alongside a live daemon resets the daemon's in-flight jobs on startup.
    """
    from callmem.core.daemon_lock import daemon_running
    from callmem.core.openai_compat import OpenAICompatClient

    if daemon_running(project_path):
        return "callmem daemon is already processing this project's queue"
    if isinstance(llm_client, OpenAICompatClient) and not llm_client.api_key:
        return "no API key for the openai_compat backend"
    return None


def create_server(
    project_path: Path, no_workers: bool = False, read_only: bool = False
) -> object:
    """Create and configure the MCP server for a project.

    When ``read_only``, mutation tools are hidden/refused and no background
    extraction workers run — a safe, query-only view of another project's
    memory (e.g. mounting one project's context inside another)."""
    from mcp.server import Server

    # read-only implies no background workers: we never mutate this DB.
    no_workers = no_workers or read_only

    config = load_config(project_path)
    db_path = project_path / ".callmem" / "memory.db"

    if not db_path.exists():
        callmem_dir = project_path / ".callmem"
        callmem_dir.mkdir(parents=True, exist_ok=True)
        config_path = callmem_dir / "config.toml"
        if not config_path.exists():
            from callmem.core.config import generate_default_config

            config_path.write_text(generate_default_config(project_path.name))

    db = Database(db_path)
    db.initialize()
    engine = MemoryEngine(db, config)

    if not no_workers:
        skip_reason = _worker_skip_reason(project_path, engine.ollama)
        if skip_reason:
            logger.warning("MCP server not starting workers: %s", skip_reason)
            no_workers = True

    if not no_workers and engine.ollama is not None:
        from callmem.core.workers import WorkerRunner

        worker = WorkerRunner(db, engine.ollama, config)
        worker.start()
    elif not no_workers:
        from callmem.core.ollama import OllamaClient

        ollama = OllamaClient(
            endpoint=config.ollama.endpoint,
            model=config.ollama.model,
            timeout=config.ollama.timeout,
        )
        from callmem.core.workers import WorkerRunner

        worker = WorkerRunner(db, ollama, config)
        worker.start()

    server = Server("callmem")
    register_tools(server, engine, read_only=read_only)
    return server


async def run_stdio(
    project_path: Path, no_workers: bool = False, read_only: bool = False
) -> None:
    """Run the MCP server on stdio transport."""
    from mcp.server.stdio import stdio_server

    server = create_server(
        project_path, no_workers=no_workers, read_only=read_only
    )
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> None:
    """CLI entry point for the MCP server."""
    parser = argparse.ArgumentParser(description="callmem MCP server")
    parser.add_argument("--project", "-p", type=Path, default=Path("."), help="Project root")
    parser.add_argument(
        "--read-only",
        action="store_true",
        help="Query-only: hide/refuse write tools and run no extraction "
        "workers (for mounting another project's memory as a context source)",
    )
    parser.add_argument(
        "--no-workers",
        action="store_true",
        help="Don't start background extraction workers",
    )
    args = parser.parse_args()

    import asyncio

    asyncio.run(
        run_stdio(
            args.project.resolve(),
            no_workers=args.no_workers,
            read_only=args.read_only,
        )
    )


if __name__ == "__main__":
    main()
