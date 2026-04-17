# WO-15: Replace htmx Polling with Server-Sent Events (SSE)

## Priority: P0

## Objective

Replace the 3-second htmx polling on the feed page with Server-Sent Events (SSE) for true real-time UI updates. New entities and session events should appear in the feed within milliseconds of being stored, not on a 3s polling cycle.

## Why

- Polling wastes bandwidth when nothing has changed (most 3s intervals return identical data)
- 3s latency feels sluggish compared to claude-mem's instant SSE updates
- SSE is a better fit: server pushes only when there's new data

## Current Architecture

- Feed page: `hx-get="/partials/feed" hx-trigger="every 3s" hx-swap="innerHTML"`
- Worker processes extraction jobs → inserts entities into DB
- UI queries DB on every poll

## Target Architecture

```
Worker extracts entity → DB insert → SSE broadcast → UI receives event → DOM update
```

### Components

### 1. SSE Event Bus (in-process)

Add a simple pub/sub event bus that the worker publishes to and SSE clients subscribe to.

Create `src/llm_mem/core/event_bus.py`:

```python
import asyncio
from dataclasses import dataclass, field

@dataclass
class EventBus:
    """In-process async event bus for SSE broadcasting."""
    _subscribers: list[asyncio.Queue] = field(default_factory=list)

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.remove(queue)

    async def publish(self, event_type: str, data: dict) -> None:
        for queue in self._subscribers:
            await queue.put({"event": event_type, "data": data})
```

Attach to `app.state.event_bus` in the FastAPI app.

### 2. SSE Endpoint

Create `src/llm_mem/ui/routes/sse.py`:

```python
@router.get("/events")
async def sse_stream(request: Request):
    """SSE endpoint for real-time feed updates."""
    from starlette.responses import StreamingResponse

    bus = request.app.state.event_bus
    queue = bus.subscribe()

    async def event_generator():
        try:
            while True:
                msg = await queue.get()
                event_type = msg["event"]
                data = json.dumps(msg["data"])
                yield f"event: {event_type}\ndata: {data}\n\n"
        except asyncio.CancelledError:
            bus.unsubscribe(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )
```

### SSE Event Types

| Event | When | Data |
|-------|------|------|
| `entity_created` | After entity extracted + inserted | Entity dict (id, type, title, content, key_points, synopsis, timestamp) |
| `session_started` | Session created | Session dict (id, started_at, agent_name) |
| `session_ended` | Session ended | Session dict (id, ended_at, summary) |
| `summary_created` | Session/chunk summary generated | Summary dict |

### 3. Worker → EventBus Integration

After the worker inserts an entity in `EntityExtractor._insert_entity()`, publish to the event bus:

```python
await self.event_bus.publish("entity_created", entity.to_row())
```

Since the worker may run in a sync context, use `asyncio.run_coroutine_threadsafe()` or have the worker post to a thread-safe queue that the async event bus drains.

### 4. Frontend: SSE Client

Replace the htmx polling div with an SSE-driven approach. Two options:

**Option A: htmx SSE extension (recommended)**
```html
<div hx-ext="sse" sse-connect="/events" sse-swap="entity_created">
  {% include "feed_partial.html" %}
</div>
```
This requires the htmx SSE extension. The server sends HTML fragments as SSE data, and htmx swaps them in.

**Option B: Vanilla JS EventSource**
```html
<script>
const source = new EventSource('/events');
source.addEventListener('entity_created', (e) => {
  const data = JSON.parse(e.data);
  const card = buildCard(data);
  document.getElementById('feed-content').prepend(card);
});
</script>
```

Option B is simpler and avoids re-rendering the entire feed — it just prepends new cards.

### 5. Fallback

Keep the htmx polling as a fallback if SSE connection fails (e.g., behind a proxy that doesn't support SSE). Use a longer interval (30s) as a catch-up mechanism.

## Files to Create

- `src/llm_mem/core/event_bus.py` — async pub/sub

## Files to Modify

- `src/llm_mem/ui/app.py` — attach event_bus to app.state, register SSE router
- `src/llm_mem/ui/routes/sse.py` — SSE streaming endpoint (or add to existing routes)
- `src/llm_mem/ui/templates/feed.html` — replace polling with SSE client
- `src/llm_mem/ui/templates/base.html` — include htmx SSE extension or JS EventSource code
- `src/llm_mem/core/extraction.py` — publish `entity_created` after insert
- `src/llm_mem/core/workers.py` — publish session events
- `src/llm_mem/adapters/opencode.py` — publish session start/end to event bus

## Acceptance Criteria

1. [ ] EventBus class with subscribe/unsubscribe/publish
2. [ ] `/events` SSE endpoint streams events to connected clients
3. [ ] New entities appear in the feed within ~100ms of extraction (no polling delay)
4. [ ] Session start/end events appear immediately
5. [ ] Multiple simultaneous SSE clients supported
6. [ ] SSE auto-reconnects on disconnect (exponential backoff, 1s → 30s)
7. [ ] htmx polling remains as 30s fallback
8. [ ] No memory leaks from disconnected clients (queues cleaned up)
9. [ ] All existing tests pass, new tests for EventBus and SSE endpoint
10. [ ] `make lint` clean, `make test` all pass
11. [ ] Committed and pushed
