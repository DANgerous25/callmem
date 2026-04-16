"""MCP server entry point.

Launches llm-mem as an MCP server over stdio (default) or SSE transport.
Usage:
    python -m llm_mem.mcp.server --project /path/to/project
"""

from __future__ import annotations

import argparse
from pathlib import Path

from llm_mem.core.config import load_config
from llm_mem.core.database import Database
from llm_mem.core.engine import MemoryEngine
from llm_mem.mcp.tools import register_tools


def create_server(
    project_path: Path, no_workers: bool = False
) -> object:
    """Create and configure the MCP server for a project."""
    from mcp.server import Server

    config = load_config(project_path)
    db_path = project_path / ".llm-mem" / "memory.db"

    if not db_path.exists():
        llm_mem_dir = project_path / ".llm-mem"
        llm_mem_dir.mkdir(parents=True, exist_ok=True)
        config_path = llm_mem_dir / "config.toml"
        if not config_path.exists():
            from llm_mem.core.config import generate_default_config

            config_path.write_text(generate_default_config(project_path.name))

    db = Database(db_path)
    db.initialize()
    engine = MemoryEngine(db, config)

    if not no_workers and engine.ollama is not None:
        from llm_mem.core.workers import WorkerRunner

        worker = WorkerRunner(db, engine.ollama, config)
        worker.start()
    elif not no_workers:
        from llm_mem.core.ollama import OllamaClient

        ollama = OllamaClient(
            endpoint=config.ollama.endpoint,
            model=config.ollama.model,
            timeout=config.ollama.timeout,
        )
        from llm_mem.core.workers import WorkerRunner

        worker = WorkerRunner(db, ollama, config)
        worker.start()

    server = Server("llm-mem")
    register_tools(server, engine)
    return server


async def run_stdio(
    project_path: Path, no_workers: bool = False
) -> None:
    """Run the MCP server on stdio transport."""
    from mcp.server.stdio import stdio_server

    server = create_server(project_path, no_workers=no_workers)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> None:
    """CLI entry point for the MCP server."""
    parser = argparse.ArgumentParser(description="llm-mem MCP server")
    parser.add_argument("--project", "-p", type=Path, default=Path("."), help="Project root")
    args = parser.parse_args()

    import asyncio

    asyncio.run(run_stdio(args.project.resolve()))


if __name__ == "__main__":
    main()
