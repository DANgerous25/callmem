"""In-process async event bus for SSE broadcasting."""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class EventBus:
    """In-process async event bus for SSE broadcasting.

    Workers publish events here; SSE endpoints subscribe and stream to clients.
    Thread-safe: all subscriber list access is protected by a lock.
    """

    _subscribers: list[asyncio.Queue] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def subscribe(self) -> asyncio.Queue:
        """Create a new subscriber queue."""
        queue: asyncio.Queue = asyncio.Queue()
        with self._lock:
            self._subscribers.append(queue)
        logger.debug("SSE subscriber added (total: %d)", len(self._subscribers))
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        """Remove a subscriber queue."""
        with self._lock:
            if queue in self._subscribers:
                self._subscribers.remove(queue)
                logger.debug(
                    "SSE subscriber removed (total: %d)",
                    len(self._subscribers),
                )

    def publish(self, event_type: str, data: dict) -> None:
        """Publish an event to all subscribers (thread-safe).

        Uses put_nowait so this can be called from sync worker threads.
        """
        dead: list[asyncio.Queue] = []
        with self._lock:
            for queue in self._subscribers:
                try:
                    queue.put_nowait({"event": event_type, "data": data})
                except asyncio.QueueFull:
                    dead.append(queue)
            for queue in dead:
                self._subscribers.remove(queue)
                logger.warning("Dropped full SSE subscriber")

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)
