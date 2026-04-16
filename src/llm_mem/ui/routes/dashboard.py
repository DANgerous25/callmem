"""Dashboard page — project overview and stats."""

from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/")
async def dashboard(request: Request) -> str:
    from llm_mem.ui.app import render_template

    engine = request.app.state.engine
    project_name = engine.config.project.name or "default"

    sessions = engine.list_sessions(limit=5)
    entities = engine.get_entities(limit=5)

    conn = engine.db.connect()
    try:
        event_count = conn.execute("SELECT COUNT(*) as c FROM events").fetchone()["c"]
        entity_count = conn.execute("SELECT COUNT(*) as c FROM entities").fetchone()["c"]
        session_count = conn.execute("SELECT COUNT(*) as c FROM sessions").fetchone()["c"]
    finally:
        conn.close()

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
async def dashboard_partial(request: Request) -> str:
    from llm_mem.ui.app import render_template

    engine = request.app.state.engine
    project_name = engine.config.project.name or "default"

    sessions = engine.list_sessions(limit=5)
    entities = engine.get_entities(limit=5)

    conn = engine.db.connect()
    try:
        event_count = conn.execute("SELECT COUNT(*) as c FROM events").fetchone()["c"]
        entity_count = conn.execute("SELECT COUNT(*) as c FROM entities").fetchone()["c"]
        session_count = conn.execute("SELECT COUNT(*) as c FROM sessions").fetchone()["c"]
    finally:
        conn.close()

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
