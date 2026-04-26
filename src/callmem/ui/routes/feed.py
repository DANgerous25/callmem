"""Memory feed — real-time card-based timeline of entities and sessions."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Request

if TYPE_CHECKING:
    from fastapi.responses import HTMLResponse

router = APIRouter()

ENTITY_TYPES = [
    "decision", "todo", "fact", "failure", "discovery",
    "feature", "bugfix", "research", "change",
]


def _resolve_event_session_map(
    engine: Any, source_event_ids: list[str],
) -> dict[str, dict[str, str | None]]:
    """Batch-resolve source_event_id -> {session_id, model_name, event_timestamp}."""
    if not source_event_ids:
        return {}

    events = engine.repo.get_events_by_ids(source_event_ids)
    event_to_session: dict[str, str | None] = {}
    event_timestamp: dict[str, str | None] = {}
    for ev in events:
        event_to_session[ev["id"]] = ev.get("session_id")
        event_timestamp[ev["id"]] = ev.get("timestamp")

    session_ids = {sid for sid in event_to_session.values() if sid}
    session_model_map: dict[str, str | None] = {}
    if session_ids:
        sessions = engine.repo.get_sessions_by_ids(list(session_ids))
        for s in sessions:
            session_model_map[s["id"]] = s.get("model_name")

    result: dict[str, dict[str, str | None]] = {}
    for eid in source_event_ids:
        sid = event_to_session.get(eid)
        result[eid] = {
            "session_id": sid,
            "model_name": session_model_map.get(sid) if sid else None,
            "event_timestamp": event_timestamp.get(eid),
        }
    return result


def _build_feed_items(
    engine: Any,
    entity_type: str | None = None,
    query: str | None = None,
    order: str = "desc",
    include_stale: bool = False,
) -> list[dict[str, Any]]:
    """Build a unified feed of entities and sessions sorted by timestamp."""
    items: list[dict[str, Any]] = []
    project_name = engine.config.project.name or "default"

    if query:
        results = engine.search(query, limit=100, include_stale=include_stale)
        entity_ids = set()
        source_event_ids: list[str] = []
        for r in results:
            eid = r.get("id", "")
            if entity_type and r.get("type") != entity_type:
                continue
            entity_ids.add(eid)
            seid = r.get("source_event_id")
            if seid:
                source_event_ids.append(seid)
            items.append({
                "kind": "entity",
                "category": r.get("type", "unknown"),
                "title": r.get("title") or r.get("content", "")[:60],
                "content": r.get("content", ""),
                "key_points": None,
                "synopsis": None,
                "timestamp": r.get("timestamp", ""),
                "id": eid,
                "status": r.get("status"),
                "priority": r.get("priority"),
                "pinned": False,
                "agent_name": None,
                "model_name": None,
                "session_id": None,
                "project_name": project_name,
                "files": [],
            })
    else:
        type_filter = entity_type if entity_type else None
        entities = engine.get_entities(
            type=type_filter, limit=100, include_stale=include_stale,
        )
        source_event_ids = []
        for e in entities:
            seid = e.get("source_event_id")
            if seid:
                source_event_ids.append(seid)

        event_session_map = _resolve_event_session_map(engine, source_event_ids)

        for e in entities:
            files = engine.repo.get_files_for_entity(e["id"])
            seid = e.get("source_event_id")
            es_map = event_session_map.get(seid, {}) if seid else {}
            items.append({
                "kind": "entity",
                "category": e["type"],
                "title": e["title"],
                "content": e.get("content", ""),
                "key_points": e.get("key_points"),
                "synopsis": e.get("synopsis"),
                "timestamp": e.get("created_at", ""),
                "id": e["id"],
                "status": e.get("status"),
                "priority": e.get("priority"),
                "pinned": e.get("pinned", False),
                "stale": bool(e.get("stale", False)),
                "superseded_by": e.get("superseded_by"),
                "staleness_reason": e.get("staleness_reason"),
                "agent_name": None,
                "model_name": es_map.get("model_name"),
                "extracted_by": e.get("extracted_by"),
                "event_timestamp": es_map.get("event_timestamp"),
                "session_id": es_map.get("session_id"),
                "project_name": project_name,
                "files": files,
            })

    if not query and not entity_type:
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
                    "model_name": s.model_name,
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
                "model_name": s.model_name,
                "project_name": project_name,
            })

    reverse = order == "desc"
    items.sort(key=lambda x: x["timestamp"], reverse=reverse)
    return items


@router.get("/")
async def feed(request: Request) -> HTMLResponse:
    from callmem.ui.app import render_template

    engine = request.app.state.engine
    project_name = engine.config.project.name or "default"
    include_stale = request.query_params.get("include_stale") == "true"

    event_count = engine.repo.count_all("events")
    entity_count = engine.repo.count_all("entities")
    session_count = engine.repo.count_all("sessions")

    items = _build_feed_items(engine, include_stale=include_stale)

    projects = engine.repo.list_projects()

    # Which LLM is currently wired for extraction — shown in the status bar
    extraction_model: str | None = None
    backend = engine.config.llm.backend
    if backend == "ollama":
        extraction_model = engine.config.ollama.model
    elif backend == "openai_compat":
        extraction_model = engine.config.openai_compat.model

    return render_template(
        request.app,
        "feed.html",
        project_name=project_name,
        event_count=event_count,
        entity_count=entity_count,
        session_count=session_count,
        items=items,
        projects=projects,
        entity_types=ENTITY_TYPES,
        active_type=None,
        active_query=None,
        active_order="desc",
        include_stale=include_stale,
        extraction_model=extraction_model,
    )


@router.get("/partials/feed")
async def feed_partial(request: Request) -> HTMLResponse:
    from callmem.ui.app import render_template

    engine = request.app.state.engine
    entity_type = request.query_params.get("type")
    query = request.query_params.get("q")
    order = request.query_params.get("order", "desc")
    include_stale = request.query_params.get("include_stale") == "true"

    items = _build_feed_items(
        engine, entity_type=entity_type, query=query, order=order,
        include_stale=include_stale,
    )

    return render_template(
        request.app,
        "feed_partial.html",
        items=items,
    )
