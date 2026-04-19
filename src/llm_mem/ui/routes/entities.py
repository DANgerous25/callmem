"""Entity browser — TODOs, decisions, facts, failures, discoveries."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

router = APIRouter()


@router.get("/entities/{entity_type}")
async def entities_by_type(
    request: Request, entity_type: str
) -> HTMLResponse:
    from llm_mem.ui.app import render_template

    engine = request.app.state.engine
    status = request.query_params.get("status")
    entities = engine.get_entities(type=entity_type, status=status, limit=100)
    return render_template(
        request.app,
        "entities.html",
        entity_type=entity_type,
        entities=entities,
        current_status=status,
    )


@router.post("/entities/{entity_id}/pin")
async def pin_entity(request: Request, entity_id: str) -> HTMLResponse:
    engine = request.app.state.engine
    engine.set_pinned(entity_id, pinned=True)
    return HTMLResponse(content="<span>Pinned</span>")


@router.post("/entities/{entity_id}/unpin")
async def unpin_entity(request: Request, entity_id: str) -> HTMLResponse:
    engine = request.app.state.engine
    engine.set_pinned(entity_id, pinned=False)
    return HTMLResponse(content="<span>Unpinned</span>")


@router.post("/entities/{entity_id}/stale")
async def mark_entity_stale(
    request: Request, entity_id: str,
) -> HTMLResponse:
    engine = request.app.state.engine
    reason = request.query_params.get("reason", "manual")
    engine.mark_stale(entity_id, reason=reason)
    return HTMLResponse(
        content="<span class='stale-indicator'>stale</span>",
    )


@router.post("/entities/{entity_id}/current")
async def mark_entity_current(
    request: Request, entity_id: str,
) -> HTMLResponse:
    engine = request.app.state.engine
    engine.mark_current(entity_id)
    return HTMLResponse(content="<span>current</span>")
