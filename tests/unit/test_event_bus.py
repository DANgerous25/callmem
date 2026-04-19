"""Tests for the SSE event bus."""

from __future__ import annotations

from callmem.core.event_bus import EventBus


class TestEventBus:
    def test_subscribe_and_unsubscribe(self) -> None:
        bus = EventBus()
        q = bus.subscribe()
        assert bus.subscriber_count == 1
        bus.unsubscribe(q)
        assert bus.subscriber_count == 0

    def test_publish_delivers_to_subscribers(self) -> None:
        bus = EventBus()
        q1 = bus.subscribe()
        q2 = bus.subscribe()
        bus.publish("entity_created", {"id": "abc123"})
        assert q1.get_nowait() == {"event": "entity_created", "data": {"id": "abc123"}}
        assert q2.get_nowait() == {"event": "entity_created", "data": {"id": "abc123"}}

    def test_unsubscribe_removes_queue(self) -> None:
        bus = EventBus()
        q1 = bus.subscribe()
        q2 = bus.subscribe()
        bus.unsubscribe(q1)
        bus.publish("test", {"x": 1})
        assert q2.get_nowait() == {"event": "test", "data": {"x": 1}}
        assert q1.empty()

    def test_publish_with_no_subscribers(self) -> None:
        bus = EventBus()
        bus.publish("test", {"x": 1})

    def test_double_unsubscribe_safe(self) -> None:
        bus = EventBus()
        q = bus.subscribe()
        bus.unsubscribe(q)
        bus.unsubscribe(q)
        assert bus.subscriber_count == 0
