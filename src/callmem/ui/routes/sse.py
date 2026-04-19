"""SSE endpoint for real-time feed updates."""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Request
from starlette.responses import StreamingResponse

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/events")
async def sse_stream(request: Request) -> StreamingResponse:
    """SSE endpoint for real-time feed updates."""
    bus = request.app.state.event_bus
    queue = bus.subscribe()

    async def event_generator() -> None:
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=30)
                except asyncio.CancelledError:
                    raise
                except (TimeoutError, asyncio.TimeoutError):
                    yield ": keepalive\n\n"
                    continue
                event_type = msg["event"]
                data = json.dumps(msg["data"])
                yield f"event: {event_type}\ndata: {data}\n\n"
        except (asyncio.CancelledError, GeneratorExit):
            pass
        finally:
            bus.unsubscribe(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/api/queue-status")
async def queue_status(request: Request) -> dict:
    """Return current queue status counts."""
    from callmem.core.queue import JobQueue

    engine = request.app.state.engine
    queue = JobQueue(engine.db)
    return queue.get_status_summary()
