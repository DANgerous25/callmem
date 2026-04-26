"""Dashboard page — project overview and stats."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Request

if TYPE_CHECKING:
    from fastapi.responses import HTMLResponse

router = APIRouter()


@router.get("/stats")
async def dashboard(request: Request) -> HTMLResponse:
    from callmem.ui.app import render_template

    engine = request.app.state.engine
    project_name = engine.config.project.name or "default"

    sessions = engine.list_sessions(limit=5)
    entities = engine.get_entities(limit=5)
    event_count = engine.repo.count_all("events")
    entity_count = engine.repo.count_all("entities")
    session_count = engine.repo.count_all("sessions")
    active_session = engine.get_active_session()

    return render_template(
        request.app,
        "dashboard.html",
        project_name=project_name,
        event_count=event_count,
        entity_count=entity_count,
        session_count=session_count,
        active_session=active_session,
        recent_sessions=sessions,
        recent_entities=entities,
    )


@router.get("/partials/dashboard")
async def dashboard_partial(request: Request) -> HTMLResponse:
    from callmem.ui.app import render_template

    engine = request.app.state.engine
    project_name = engine.config.project.name or "default"

    sessions = engine.list_sessions(limit=5)
    entities = engine.get_entities(limit=5)
    event_count = engine.repo.count_all("events")
    entity_count = engine.repo.count_all("entities")
    session_count = engine.repo.count_all("sessions")
    active_session = engine.get_active_session()

    return render_template(
        request.app,
        "dashboard_partial.html",
        project_name=project_name,
        event_count=event_count,
        entity_count=entity_count,
        session_count=session_count,
        active_session=active_session,
        recent_sessions=sessions,
        recent_entities=entities,
    )
