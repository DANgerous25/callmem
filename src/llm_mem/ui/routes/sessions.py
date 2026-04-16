"""Sessions list and detail pages."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

router = APIRouter()


@router.get("/sessions")
async def sessions_list(request: Request) -> HTMLResponse:
    from llm_mem.ui.app import render_template

    engine = request.app.state.engine
    sessions = engine.list_sessions(limit=50)
    return render_template(
        request.app, "sessions.html", sessions=sessions
    )


@router.get("/sessions/{session_id}")
async def session_detail(request: Request, session_id: str) -> HTMLResponse:
    from llm_mem.ui.app import render_template

    engine = request.app.state.engine
    session = engine.get_session(session_id)
    if session is None:
        return render_template(
            request.app, "session_detail.html",
            session=None, events=[], entities=[],
        )

    events = engine.get_events(session_id=session_id, limit=100)
    entities = engine.get_entities(limit=50)

    session_entities = [
        e for e in entities
        if e.get("source_event_id") in [ev.id for ev in events]
    ]

    return render_template(
        request.app,
        "session_detail.html",
        session=session,
        events=events,
        entities=session_entities,
    )


@router.get("/partials/sessions")
async def sessions_partial(request: Request) -> HTMLResponse:
    from llm_mem.ui.app import render_template

    engine = request.app.state.engine
    sessions = engine.list_sessions(limit=50)
    return render_template(
        request.app, "sessions_partial.html", sessions=sessions
    )


@router.get("/partials/sessions/{session_id}")
async def session_detail_partial(request: Request, session_id: str) -> HTMLResponse:
    from llm_mem.ui.app import render_template

    engine = request.app.state.engine
    session = engine.get_session(session_id)
    if session is None:
        return HTMLResponse("<p>Session not found.</p>")

    events = engine.get_events(session_id=session_id, limit=100)
    entities = engine.get_entities(limit=50)

    session_entities = [
        e for e in entities
        if e.get("source_event_id") in [ev.id for ev in events]
    ]

    return render_template(
        request.app,
        "session_detail_partial.html",
        session=session,
        events=events,
        entities=session_entities,
    )
