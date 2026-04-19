"""FastAPI application factory for the callmem web UI."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader

if TYPE_CHECKING:
    from callmem.core.engine import MemoryEngine

from callmem.compat import UTC


def _relative_time(ts: str) -> str:
    """Convert an ISO timestamp to a human-friendly relative or local string."""
    if not ts:
        return ""
    try:
        clean = ts.replace("Z", "+00:00")
        dt = datetime.fromisoformat(clean)
        if dt.tzinfo is None:
            from datetime import timezone
            dt = dt.replace(tzinfo=timezone.utc)
        now = datetime.now(UTC)
        diff = now - dt
        total_secs = int(diff.total_seconds())
        if total_secs < 0:
            total_secs = 0
        if total_secs < 60:
            return "just now"
        if total_secs < 3600:
            mins = total_secs // 60
            return f"{mins}m ago"
        if total_secs < 86400:
            hours = total_secs // 3600
            return f"{hours}h ago"
        if total_secs < 604800:
            days = total_secs // 86400
            return f"{days}d ago"
        return dt.strftime("%b %d, %I:%M %p")
    except (ValueError, TypeError):
        return ts[:19].replace("T", " ")


def create_app(engine: MemoryEngine) -> FastAPI:
    """Create and configure the FastAPI application."""
    from callmem.core.event_bus import EventBus

    app = FastAPI(title="callmem", docs_url=None, redoc_url=None)

    templates_dir = Path(__file__).parent / "templates"
    static_dir = Path(__file__).parent / "static"

    env = Environment(
        loader=FileSystemLoader(str(templates_dir)),
        autoescape=True,
    )
    def _basename(path: str) -> str:
        return path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1] if path else ""

    env.filters["basename"] = _basename
    env.filters["format_number"] = lambda n: f"{n:,}" if isinstance(n, int) else str(n)
    env.filters["relative_time"] = _relative_time

    app.state.engine = engine
    app.state.templates = env
    app.state.event_bus = EventBus()

    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    from callmem.ui.routes.briefing import router as briefing_router
    from callmem.ui.routes.dashboard import router as dashboard_router
    from callmem.ui.routes.entities import router as entities_router
    from callmem.ui.routes.feed import router as feed_router
    from callmem.ui.routes.search import router as search_router
    from callmem.ui.routes.sessions import router as sessions_router
    from callmem.ui.routes.settings import router as settings_router
    from callmem.ui.routes.sse import router as sse_router

    app.include_router(feed_router)
    app.include_router(dashboard_router)
    app.include_router(sessions_router)
    app.include_router(search_router)
    app.include_router(entities_router)
    app.include_router(briefing_router)
    app.include_router(settings_router)
    app.include_router(sse_router)

    return app


def render_template(
    app: FastAPI, name: str, **context: object
) -> HTMLResponse:
    """Render a Jinja2 template with the given context."""
    env: Environment = app.state.templates
    template = env.get_template(name)
    return HTMLResponse(template.render(**context))
