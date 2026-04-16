"""Memory feed — real-time card-based timeline of entities and sessions."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Request

if TYPE_CHECKING:
    from fastapi.responses import HTMLResponse

router = APIRouter()


def _build_feed_items(engine: Any) -> list[dict[str, Any]]:
    """Build a unified feed of entities and sessions sorted by timestamp desc."""
    items: list[dict[str, Any]] = []

    project_name = engine.config.project.name or "default"

    # Entities — primary feed items
    entities = engine.get_entities(limit=100)
    for e in entities:
        items.append({
            "kind": "entity",
            "category": e["type"],
            "title": e["title"],
            "content": e.get("content", ""),
            "timestamp": e.get("created_at", ""),
            "id": e["id"],
            "status": e.get("status"),
            "priority": e.get("priority"),
            "pinned": e.get("pinned", False),
            "agent_name": None,
            "project_name": project_name,
        })

    # Sessions
    sessions = engine.list_sessions(limit=20)
    for s in sessions:
        if s.summary:
            items.append({
                "kind": "summary",
                "category": "summary",
                "title": "Session Summary",
                "content": s.summary,
                "timestamp": s.ended_at or s.started_at,
                "id": s.id,
                "status": s.status,
                "priority": None,
                "pinned": False,
                "agent_name": s.agent_name,
                "project_name": project_name,
            })

        items.append({
            "kind": "session",
            "category": "session",
            "title": f"Session {'started' if s.status == 'active' else s.status}",
            "content": f"{s.event_count} events" + (
                f" \u00b7 {s.agent_name}" if s.agent_name else ""
            ),
            "timestamp": s.started_at,
            "id": s.id,
            "status": s.status,
            "priority": None,
            "pinned": False,
            "agent_name": s.agent_name,
            "project_name": project_name,
        })

    items.sort(key=lambda x: x["timestamp"], reverse=True)
    return items


@router.get("/")
async def feed(request: Request) -> HTMLResponse:
    from llm_mem.ui.app import render_template

    engine = request.app.state.engine
    project_name = engine.config.project.name or "default"

    conn = engine.db.connect()
    try:
        event_count = conn.execute("SELECT COUNT(*) as c FROM events").fetchone()["c"]
        entity_count = conn.execute("SELECT COUNT(*) as c FROM entities").fetchone()["c"]
        session_count = conn.execute("SELECT COUNT(*) as c FROM sessions").fetchone()["c"]
    finally:
        conn.close()

    items = _build_feed_items(engine)

    return render_template(
        request.app,
        "feed.html",
        project_name=project_name,
        event_count=event_count,
        entity_count=entity_count,
        session_count=session_count,
        items=items,
    )


@router.get("/partials/feed")
async def feed_partial(request: Request) -> HTMLResponse:
    from llm_mem.ui.app import render_template

    engine = request.app.state.engine
    items = _build_feed_items(engine)

    return render_template(
        request.app,
        "feed_partial.html",
        items=items,
    )
