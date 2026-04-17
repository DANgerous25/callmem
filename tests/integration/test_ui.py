"""Integration tests for the web UI."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi.testclient import TestClient

from llm_mem.core.engine import MemoryEngine
from llm_mem.models.config import Config
from llm_mem.ui.app import create_app

if TYPE_CHECKING:
    from llm_mem.core.database import Database
    pass


def _make_client(memory_db: Database) -> TestClient:
    config = Config(sensitive_data={"enabled": False, "llm_scan": False})
    engine = MemoryEngine(memory_db, config)
    app = create_app(engine)
    return TestClient(app)


def _make_client_with_data(memory_db: Database) -> TestClient:
    config = Config(sensitive_data={"enabled": False, "llm_scan": False})
    engine = MemoryEngine(memory_db, config)
    session = engine.start_session()
    engine.ingest_one("note", "Add cursor-based pagination to list endpoints")
    engine.ingest_one("note", "Redis caching for auth module")
    engine.end_session(session.id)

    from llm_mem.models.entities import Entity

    entity = Entity(
        project_id=engine.project_id,
        source_event_id=None,
        type="todo",
        title="Add pagination tests",
        content="Write integration tests for cursor pagination",
        status="open",
        priority="high",
    )
    conn = memory_db.connect()
    try:
        row = entity.to_row()
        conn.execute(
            "INSERT INTO entities "
            "(id, project_id, source_event_id, type, title, content, "
            "status, priority, pinned, created_at, updated_at, "
            "resolved_at, metadata, archived_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row["id"], row["project_id"], row["source_event_id"],
                row["type"], row["title"], row["content"],
                row["status"], row["priority"], row["pinned"],
                row["created_at"], row["updated_at"],
                row["resolved_at"], row["metadata"], row["archived_at"],
            ),
        )
        conn.commit()
    finally:
        conn.close()

    app = create_app(engine)
    return TestClient(app)


class TestDashboard:
    def test_dashboard_loads(self, memory_db: Database) -> None:
        client = _make_client(memory_db)
        response = client.get("/stats")
        assert response.status_code == 200
        assert "llm-mem" in response.text

    def test_dashboard_shows_stats(self, memory_db: Database) -> None:
        client = _make_client_with_data(memory_db)
        response = client.get("/stats")
        assert response.status_code == 200
        assert "Events:" in response.text or "event_count" in response.text.lower()


class TestFeed:
    def test_feed_loads(self, memory_db: Database) -> None:
        client = _make_client(memory_db)
        response = client.get("/")
        assert response.status_code == 200
        assert "llm-mem" in response.text

    def test_feed_shows_stats(self, memory_db: Database) -> None:
        client = _make_client_with_data(memory_db)
        response = client.get("/")
        assert response.status_code == 200
        assert "Events:" in response.text

    def test_feed_shows_entity_cards(self, memory_db: Database) -> None:
        client = _make_client_with_data(memory_db)
        response = client.get("/")
        assert response.status_code == 200
        assert "feed-card" in response.text
        assert "TODO" in response.text
        assert "Add pagination tests" in response.text

    def test_feed_shows_session_cards(self, memory_db: Database) -> None:
        client = _make_client_with_data(memory_db)
        response = client.get("/")
        assert response.status_code == 200
        assert "SESSION" in response.text
        assert "ended" in response.text.lower()

    def test_feed_has_sse_and_fallback(self, memory_db: Database) -> None:
        client = _make_client(memory_db)
        html = _get_html(client.get("/"))
        assert "EventSource" in html
        assert "/events" in html
        assert "/partials/feed" in html

    def test_feed_empty_state(self, memory_db: Database) -> None:
        client = _make_client(memory_db)
        response = client.get("/")
        assert response.status_code == 200
        assert "No memory items yet" in response.text


class TestFeedPartial:
    def test_partial_returns_fragment(self, memory_db: Database) -> None:
        client = _make_client(memory_db)
        response = client.get("/partials/feed")
        assert response.status_code == 200
        assert "<!DOCTYPE" not in response.text
        assert "<html" not in response.text

    def test_partial_with_data(self, memory_db: Database) -> None:
        client = _make_client_with_data(memory_db)
        response = client.get("/partials/feed")
        assert response.status_code == 200
        assert "feed-card" in response.text
        assert "TODO" in response.text

    def test_partial_shows_badges(self, memory_db: Database) -> None:
        client = _make_client_with_data(memory_db)
        response = client.get("/partials/feed")
        assert response.status_code == 200
        assert "badge-todo" in response.text
        assert "badge-session" in response.text

    def test_partial_shows_status(self, memory_db: Database) -> None:
        client = _make_client_with_data(memory_db)
        response = client.get("/partials/feed")
        assert response.status_code == 200
        assert "status-open" in response.text


class TestSessions:
    def test_sessions_list(self, memory_db: Database) -> None:
        client = _make_client_with_data(memory_db)
        response = client.get("/sessions")
        assert response.status_code == 200

    def test_session_detail(self, memory_db: Database) -> None:
        config = Config(sensitive_data={"enabled": False, "llm_scan": False})
        engine = MemoryEngine(memory_db, config)
        session = engine.start_session()
        engine.ingest_one("note", "test event")
        engine.end_session(session.id)

        client = TestClient(create_app(engine))
        response = client.get(f"/sessions/{session.id}")
        assert response.status_code == 200


class TestDashboardPartial:
    def test_partial_returns_fragment(self, memory_db: Database) -> None:
        client = _make_client(memory_db)
        response = client.get("/partials/dashboard")
        assert response.status_code == 200
        # Partial should NOT contain full HTML page structure
        assert "<!DOCTYPE" not in response.text
        assert "<html" not in response.text
        # But should contain dashboard content
        assert "Events:" in response.text

    def test_partial_with_data(self, memory_db: Database) -> None:
        client = _make_client_with_data(memory_db)
        response = client.get("/partials/dashboard")
        assert response.status_code == 200
        assert "Recent Sessions" in response.text


class TestSessionsPartial:
    def test_partial_returns_fragment(self, memory_db: Database) -> None:
        client = _make_client(memory_db)
        response = client.get("/partials/sessions")
        assert response.status_code == 200
        assert "<!DOCTYPE" not in response.text
        assert "Sessions" in response.text

    def test_partial_with_data(self, memory_db: Database) -> None:
        client = _make_client_with_data(memory_db)
        response = client.get("/partials/sessions")
        assert response.status_code == 200
        assert "<table>" in response.text


class TestSessionDetailPartial:
    def test_partial_not_found(self, memory_db: Database) -> None:
        client = _make_client(memory_db)
        response = client.get("/partials/sessions/nonexistent")
        assert response.status_code == 200
        assert "not found" in response.text

    def test_partial_with_session(self, memory_db: Database) -> None:
        config = Config(sensitive_data={"enabled": False, "llm_scan": False})
        engine = MemoryEngine(memory_db, config)
        session = engine.start_session()
        engine.ingest_one("note", "test event for partial")
        engine.end_session(session.id)

        client = TestClient(create_app(engine))
        response = client.get(f"/partials/sessions/{session.id}")
        assert response.status_code == 200
        assert "<!DOCTYPE" not in response.text
        assert session.id[:8] in response.text


def _get_html(response: object) -> str:
    """Extract HTML from response, handling JSON-wrapped strings."""
    ct = response.headers.get("content-type", "")
    if ct.startswith("application/json"):
        return response.json()
    return response.text


class TestHtmxPollingAttributes:
    def test_dashboard_has_polling_div(self, memory_db: Database) -> None:
        client = _make_client(memory_db)
        html = _get_html(client.get("/stats"))
        assert "hx-get" in html
        assert "/partials/dashboard" in html
        assert "every 3s" in html

    def test_sessions_has_polling_div(self, memory_db: Database) -> None:
        client = _make_client(memory_db)
        html = _get_html(client.get("/sessions"))
        assert "hx-get" in html
        assert "/partials/sessions" in html
        assert "every 3s" in html

    def test_session_detail_has_polling_div(
        self, memory_db: Database
    ) -> None:
        config = Config(
            sensitive_data={"enabled": False, "llm_scan": False}
        )
        engine = MemoryEngine(memory_db, config)
        session = engine.start_session()
        engine.ingest_one("note", "test")
        engine.end_session(session.id)

        client = TestClient(create_app(engine))
        html = _get_html(client.get(f"/sessions/{session.id}"))
        assert "hx-get" in html
        assert f"/partials/sessions/{session.id}" in html
        assert "every 3s" in html

    def test_session_not_found_no_polling(
        self, memory_db: Database
    ) -> None:
        client = _make_client(memory_db)
        html = _get_html(client.get("/sessions/nonexistent"))
        assert "hx-get" not in html


class TestSearch:
    def test_search_page_loads(self, memory_db: Database) -> None:
        client = _make_client(memory_db)
        response = client.get("/search")
        assert response.status_code == 200

    def test_search_with_query(self, memory_db: Database) -> None:
        client = _make_client_with_data(memory_db)
        response = client.get("/search?q=pagination")
        assert response.status_code == 200

    def test_search_empty_results(self, memory_db: Database) -> None:
        client = _make_client_with_data(memory_db)
        response = client.get("/search?q=nonexistent_xyzzy")
        assert response.status_code == 200


class TestEntities:
    def test_entities_todos(self, memory_db: Database) -> None:
        client = _make_client_with_data(memory_db)
        response = client.get("/entities/todo")
        assert response.status_code == 200

    def test_entities_decisions(self, memory_db: Database) -> None:
        client = _make_client_with_data(memory_db)
        response = client.get("/entities/decision")
        assert response.status_code == 200

    def test_pin_entity(self, memory_db: Database) -> None:
        config = Config(sensitive_data={"enabled": False, "llm_scan": False})
        engine = MemoryEngine(memory_db, config)

        from llm_mem.models.entities import Entity

        entity = Entity(
            project_id=engine.project_id,
            type="todo",
            title="Test todo",
            content="Content",
            status="open",
        )
        conn = memory_db.connect()
        try:
            row = entity.to_row()
            conn.execute(
                "INSERT INTO entities "
                "(id, project_id, source_event_id, type, title, content, "
                "status, priority, pinned, created_at, updated_at, "
                "resolved_at, metadata, archived_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    row["id"], row["project_id"], row["source_event_id"],
                    row["type"], row["title"], row["content"],
                    row["status"], row["priority"], row["pinned"],
                    row["created_at"], row["updated_at"],
                    row["resolved_at"], row["metadata"], row["archived_at"],
                ),
            )
            conn.commit()
        finally:
            conn.close()

        client = TestClient(create_app(engine))
        response = client.post(f"/entities/{entity.id}/pin")
        assert response.status_code == 200


class TestBriefing:
    def test_briefing_page(self, memory_db: Database) -> None:
        client = _make_client_with_data(memory_db)
        response = client.get("/briefing")
        assert response.status_code == 200

    def test_briefing_with_focus(self, memory_db: Database) -> None:
        client = _make_client_with_data(memory_db)
        response = client.get("/briefing?focus=auth")
        assert response.status_code == 200
