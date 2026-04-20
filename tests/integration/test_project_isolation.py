"""Cross-project data isolation guarantees.

The llm-mem → callmem migration history had multiple incidents where
one project's events/entities leaked into another's queries. These
tests pin the project_id filter contract on every path that touches
per-project data (ingest, search, auto-resolve, sweep, briefing).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import patch

from callmem.core.engine import MemoryEngine
from callmem.core.extraction import EntityExtractor
from callmem.core.ollama import OllamaClient
from callmem.models.config import Config

if TYPE_CHECKING:
    from callmem.core.database import Database


def _extraction_json(**buckets: list[dict]) -> str:
    payload = {
        k: [] for k in (
            "decisions", "todos", "facts", "failures", "discoveries",
            "features", "bugfixes", "research", "changes",
        )
    }
    payload.update(buckets)
    return json.dumps(payload)


def _make_engine(memory_db: Database, name: str) -> MemoryEngine:
    config = Config(
        project={"name": name},
        sensitive_data={"enabled": False, "llm_scan": False},
    )
    return MemoryEngine(memory_db, config)


class TestTwoProjectsOneDatabase:
    """Two projects sharing a DB must stay isolated in every read path."""

    def test_projects_get_distinct_ids(self, memory_db: Database) -> None:
        alpha = _make_engine(memory_db, "alpha")
        beta = _make_engine(memory_db, "beta")
        assert alpha.project_id != beta.project_id

    def test_events_are_project_scoped(self, memory_db: Database) -> None:
        alpha = _make_engine(memory_db, "alpha")
        beta = _make_engine(memory_db, "beta")

        alpha.start_session()
        alpha.ingest_one("note", "alpha-only content")
        beta.start_session()
        beta.ingest_one("note", "beta-only content")

        alpha_events = alpha.get_events()
        beta_events = beta.get_events()

        assert len(alpha_events) == 1
        assert len(beta_events) == 1
        assert alpha_events[0].content == "alpha-only content"
        assert beta_events[0].content == "beta-only content"

    def test_sessions_are_project_scoped(self, memory_db: Database) -> None:
        alpha = _make_engine(memory_db, "alpha")
        beta = _make_engine(memory_db, "beta")
        s_a = alpha.start_session(agent_name="alpha-agent")
        s_b = beta.start_session(agent_name="beta-agent")

        assert [s.id for s in alpha.list_sessions()] == [s_a.id]
        assert [s.id for s in beta.list_sessions()] == [s_b.id]

    def test_search_does_not_cross_projects(
        self, memory_db: Database,
    ) -> None:
        alpha = _make_engine(memory_db, "alpha")
        beta = _make_engine(memory_db, "beta")

        alpha.start_session()
        alpha.ingest_one(
            "decision", "Alpha uses Redis for session store",
        )
        beta.start_session()
        beta.ingest_one(
            "decision", "Beta uses Postgres for session store",
        )

        alpha_results = alpha.search("session store")
        beta_results = beta.search("session store")

        assert all(
            "Alpha" in r["content"] for r in alpha_results
        ), f"alpha leaked beta data: {alpha_results}"
        assert all(
            "Beta" in r["content"] for r in beta_results
        ), f"beta leaked alpha data: {beta_results}"

    def test_get_entities_does_not_cross_projects(
        self, memory_db: Database,
    ) -> None:
        alpha = _make_engine(memory_db, "alpha")
        beta = _make_engine(memory_db, "beta")
        from callmem.models.entities import Entity

        def _insert(engine: MemoryEngine, title: str) -> str:
            entity = Entity(
                project_id=engine.project_id, type="todo",
                title=title, content=title, status="open",
            )
            row = entity.to_row()
            conn = memory_db.connect()
            try:
                conn.execute(
                    "INSERT INTO entities "
                    "(id, project_id, source_event_id, type, title, "
                    "content, key_points, synopsis, status, priority, "
                    "pinned, created_at, updated_at, resolved_at, "
                    "metadata, archived_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        row["id"], row["project_id"],
                        row["source_event_id"], row["type"], row["title"],
                        row["content"], row["key_points"], row["synopsis"],
                        row["status"], row["priority"], row["pinned"],
                        row["created_at"], row["updated_at"],
                        row["resolved_at"], row["metadata"],
                        row["archived_at"],
                    ),
                )
                conn.commit()
            finally:
                conn.close()
            return entity.id

        _insert(alpha, "Alpha-only TODO")
        _insert(beta, "Beta-only TODO")

        alpha_todos = alpha.get_entities(type="todo")
        beta_todos = beta.get_entities(type="todo")

        assert [t["title"] for t in alpha_todos] == ["Alpha-only TODO"]
        assert [t["title"] for t in beta_todos] == ["Beta-only TODO"]


class TestAutoResolveIsolation:
    """Auto-resolve and sweep must not cross project boundaries."""

    def test_feature_in_project_a_does_not_close_todo_in_project_b(
        self, memory_db: Database,
    ) -> None:
        alpha = _make_engine(memory_db, "alpha")
        beta = _make_engine(memory_db, "beta")
        extractor = EntityExtractor(memory_db, OllamaClient())

        # Beta has an open TODO with a title a feature could match on
        beta.start_session()
        beta.ingest_one("note", "need to wire up pagination")
        beta_todo = _extraction_json(todos=[{
            "title": "Add cursor-based pagination for list endpoints",
            "content": "Paginate list responses.", "status": "open",
            "priority": "medium", "key_points": ["cursor tokens"],
            "synopsis": "Pagination plan.",
        }])
        with patch.object(extractor.ollama, "_generate", return_value=beta_todo):
            extractor.process_pending()

        # Alpha ships a feature with a very similar title — must NOT
        # resolve beta's TODO.
        alpha.start_session()
        alpha.ingest_one("note", "shipped cursor pagination in alpha service")
        alpha_feature = _extraction_json(features=[{
            "title": "Cursor-based pagination for list endpoints",
            "content": "Alpha cursor pagination.",
            "key_points": ["alpha impl"],
            "synopsis": "Alpha shipped pagination.",
        }])
        with patch.object(extractor.ollama, "_generate", return_value=alpha_feature):
            extractor.process_pending()

        conn = memory_db.connect()
        try:
            row = conn.execute(
                "SELECT status FROM entities "
                "WHERE project_id = ? AND type = 'todo'",
                (beta.project_id,),
            ).fetchone()
        finally:
            conn.close()
        assert row["status"] == "open", (
            "beta's TODO was resolved by an alpha feature — project "
            "isolation broken in auto-resolve"
        )

    def test_sweep_only_touches_target_project(
        self, memory_db: Database,
    ) -> None:
        from callmem.models.entities import Entity

        alpha = _make_engine(memory_db, "alpha")
        beta = _make_engine(memory_db, "beta")
        extractor = EntityExtractor(memory_db, OllamaClient())

        # Both projects get an identical driver + TODO pair
        for engine in (alpha, beta):
            feature = Entity(
                project_id=engine.project_id, type="feature",
                title="Cursor-based pagination for list endpoints",
                content="Pagination shipped.",
            )
            todo = Entity(
                project_id=engine.project_id, type="todo",
                title="Add cursor-based pagination for list endpoints",
                content="Pagination plan.", status="open",
            )
            conn = memory_db.connect()
            try:
                for e in (feature, todo):
                    row = e.to_row()
                    conn.execute(
                        "INSERT INTO entities "
                        "(id, project_id, source_event_id, type, title, "
                        "content, key_points, synopsis, status, priority, "
                        "pinned, created_at, updated_at, resolved_at, "
                        "metadata, archived_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            row["id"], row["project_id"],
                            row["source_event_id"], row["type"],
                            row["title"], row["content"],
                            row["key_points"], row["synopsis"],
                            row["status"], row["priority"], row["pinned"],
                            row["created_at"], row["updated_at"],
                            row["resolved_at"], row["metadata"],
                            row["archived_at"],
                        ),
                    )
                conn.commit()
            finally:
                conn.close()

        # Sweep only alpha — beta's TODO must remain open
        records = extractor.sweep_resolutions(alpha.project_id)
        assert all(
            r["id"] != "" for r in records
        )  # records all reference closed items

        conn = memory_db.connect()
        try:
            statuses = {
                r["project_id"]: r["status"]
                for r in conn.execute(
                    "SELECT project_id, status FROM entities "
                    "WHERE type = 'todo'",
                ).fetchall()
            }
        finally:
            conn.close()
        assert statuses[alpha.project_id] == "done"
        assert statuses[beta.project_id] == "open"


class TestBriefingIsolation:
    def test_briefing_only_shows_target_project(
        self, memory_db: Database,
    ) -> None:
        alpha = _make_engine(memory_db, "alpha")
        beta = _make_engine(memory_db, "beta")
        extractor = EntityExtractor(memory_db, OllamaClient())

        alpha.start_session()
        alpha.ingest_one("note", "alpha has its own TODO")
        alpha_resp = _extraction_json(todos=[{
            "title": "Alpha-exclusive pagination work",
            "content": "Alpha only.", "status": "open", "priority": "high",
            "key_points": ["alpha scope"],
            "synopsis": "Alpha's only TODO.",
        }])
        with patch.object(extractor.ollama, "_generate", return_value=alpha_resp):
            extractor.process_pending()

        beta.start_session()
        beta.ingest_one("note", "beta has its own TODO")
        beta_resp = _extraction_json(todos=[{
            "title": "Beta-exclusive caching work",
            "content": "Beta only.", "status": "open", "priority": "high",
            "key_points": ["beta scope"],
            "synopsis": "Beta's only TODO.",
        }])
        with patch.object(extractor.ollama, "_generate", return_value=beta_resp):
            extractor.process_pending()

        alpha_briefing = alpha.get_briefing()["content"]
        beta_briefing = beta.get_briefing()["content"]

        assert "Alpha-exclusive" in alpha_briefing
        assert "Beta-exclusive" not in alpha_briefing
        assert "Beta-exclusive" in beta_briefing
        assert "Alpha-exclusive" not in beta_briefing
