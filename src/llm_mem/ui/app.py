"""FastAPI application factory for the llm-mem web UI."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader

if TYPE_CHECKING:
    from llm_mem.core.engine import MemoryEngine


def create_app(engine: MemoryEngine) -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(title="llm-mem", docs_url=None, redoc_url=None)

    templates_dir = Path(__file__).parent / "templates"
    static_dir = Path(__file__).parent / "static"

    env = Environment(
        loader=FileSystemLoader(str(templates_dir)),
        autoescape=True,
    )

    app.state.engine = engine
    app.state.templates = env

    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    from llm_mem.ui.routes.briefing import router as briefing_router
    from llm_mem.ui.routes.dashboard import router as dashboard_router
    from llm_mem.ui.routes.entities import router as entities_router
    from llm_mem.ui.routes.search import router as search_router
    from llm_mem.ui.routes.sessions import router as sessions_router

    app.include_router(dashboard_router)
    app.include_router(sessions_router)
    app.include_router(search_router)
    app.include_router(entities_router)
    app.include_router(briefing_router)

    return app


def render_template(
    app: FastAPI, name: str, **context: object
) -> HTMLResponse:
    """Render a Jinja2 template with the given context."""
    env: Environment = app.state.templates
    template = env.get_template(name)
    return HTMLResponse(template.render(**context))
