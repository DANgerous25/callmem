"""HTTP ingest endpoint for push-based event capture.

Accepts events from OpenCode plugins and Claude Code hooks via POST.
This is the primary capture path — replaces polling-based adapters.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from callmem.core.engine import MemoryEngine

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["ingest"])


class IngestEvent(BaseModel):
    """Single event from a plugin/hook."""

    type: str
    content: str
    timestamp: str | None = None
    metadata: dict[str, Any] | None = None


class IngestRequest(BaseModel):
    """Batch of events with optional session lifecycle."""

    events: list[IngestEvent] = Field(default_factory=list)
    session_id: str | None = None
    agent_name: str | None = None
    session_action: str | None = Field(
        default=None,
        description="start, end, or None",
    )
    session_note: str | None = None


class IngestResponse(BaseModel):
    """Result of an ingest request."""

    ingested: int
    session_id: str | None = None
    error: str | None = None


@router.post("/ingest", response_model=IngestResponse)
async def ingest_events(request: Request, body: IngestRequest) -> JSONResponse:
    """Accept events from OpenCode plugins or Claude Code hooks.

    Handles session lifecycle (start/end) and event ingest in a single
    request. The daemon's worker picks up extraction jobs asynchronously.
    """
    engine: MemoryEngine = request.app.state.engine

    sid = body.session_id

    if body.session_action == "start":
        session = engine.start_session(
            agent_name=body.agent_name or "opencode",
        )
        sid = session.id
    elif body.session_action == "end" and sid:
        try:
            engine.end_session(sid, note=body.session_note)
        except (ValueError, Exception) as exc:
            logger.warning("Failed to end session %s: %s", sid, exc)

    if not body.events:
        return JSONResponse({"ingested": 0, "session_id": sid})

    from callmem.models.events import EventInput

    inputs = [
        EventInput(
            type=e.type,
            content=e.content,
            timestamp=e.timestamp,
            metadata=e.metadata,
        )
        for e in body.events
        if e.content
    ]

    if not inputs:
        return JSONResponse({"ingested": 0, "session_id": sid})

    try:
        stored = engine.ingest(inputs, session_id=sid)
        return JSONResponse({"ingested": len(stored), "session_id": sid})
    except Exception as exc:
        logger.error("Ingest failed: %s", exc)
        return JSONResponse(
            {"ingested": 0, "session_id": sid, "error": str(exc)},
            status_code=500,
        )


@router.get("/health")
async def health(request: Request) -> dict[str, str]:
    """Health check for plugin/hook connectivity."""
    engine: MemoryEngine = request.app.state.engine
    return {
        "status": "ok",
        "project_id": engine.project_id,
    }
