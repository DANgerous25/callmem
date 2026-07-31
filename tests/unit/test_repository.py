"""Tests for the Repository data access layer."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from callmem.core.repository import Repository
from callmem.models.entities import Entity
from callmem.models.events import Event
from callmem.models.projects import Project
from callmem.models.sessions import Session

if TYPE_CHECKING:
    from callmem.core.database import Database
    from callmem.core.engine import MemoryEngine


HOSTILE_QUERIES = [
    "cookie-backed",
    '"quoted phrase"',
    "AND)(",
    "a NOT b OR",
    "()()",
]


class TestProjectQueries:
    def test_create_and_get_project(self, memory_db: Database) -> None:
        repo = Repository(memory_db)
        project = Project(name="test-project", root_path="/tmp/test")
        repo.create_project(project)
        fetched = repo.get_project(project.id)
        assert fetched is not None
        assert fetched.name == "test-project"
        assert fetched.root_path == "/tmp/test"

    def test_get_project_not_found(self, memory_db: Database) -> None:
        repo = Repository(memory_db)
        assert repo.get_project("nonexistent") is None

    def test_resolve_project_root_no_self_heal_for_nonstandard_db_path(
        self, tmp_path,
    ) -> None:
        """A db_path that doesn't sit under a .callmem directory must
        never yield a derived root — a shallow/wrong root would
        trivially pass anchor-containment checks. No self-heal write
        may happen either."""
        from callmem.core.database import Database

        db = Database(tmp_path / "test.db")
        db.initialize()
        repo = Repository(db)
        project = Project(name="test-project")  # root_path NULL
        repo.create_project(project)

        assert repo.resolve_project_root(project.id) is None
        stored = repo.get_project(project.id)
        assert stored is not None
        assert stored.root_path is None

    def test_get_project_by_name(self, memory_db: Database) -> None:
        repo = Repository(memory_db)
        project = Project(name="unique-name")
        repo.create_project(project)
        fetched = repo.get_project_by_name("unique-name")
        assert fetched is not None
        assert fetched.id == project.id

    def test_get_project_by_name_not_found(self, memory_db: Database) -> None:
        repo = Repository(memory_db)
        assert repo.get_project_by_name("nope") is None


class TestSessionQueries:
    def test_insert_and_get_session(self, memory_db: Database) -> None:
        repo = Repository(memory_db)
        project = Project(name="p")
        repo.create_project(project)
        session = Session(project_id=project.id, agent_name="test")
        repo.insert_session(session)
        fetched = repo.get_session(session.id)
        assert fetched is not None
        assert fetched.agent_name == "test"
        assert fetched.status == "active"

    def test_get_session_not_found(self, memory_db: Database) -> None:
        repo = Repository(memory_db)
        assert repo.get_session("nonexistent") is None

    def test_update_session(self, memory_db: Database) -> None:
        repo = Repository(memory_db)
        project = Project(name="p")
        repo.create_project(project)
        session = Session(project_id=project.id)
        repo.insert_session(session)

        session.status = "ended"
        session.event_count = 5
        repo.update_session(session)

        fetched = repo.get_session(session.id)
        assert fetched is not None
        assert fetched.status == "ended"
        assert fetched.event_count == 5

    def test_get_active_session(self, memory_db: Database) -> None:
        repo = Repository(memory_db)
        project = Project(name="p")
        repo.create_project(project)

        assert repo.get_active_session(project.id) is None

        session = Session(project_id=project.id)
        repo.insert_session(session)
        active = repo.get_active_session(project.id)
        assert active is not None
        assert active.id == session.id

    def test_get_active_session_returns_none_after_end(
        self, memory_db: Database
    ) -> None:
        repo = Repository(memory_db)
        project = Project(name="p")
        repo.create_project(project)
        session = Session(project_id=project.id)
        repo.insert_session(session)

        session.status = "ended"
        repo.update_session(session)

        assert repo.get_active_session(project.id) is None

    def test_list_sessions(self, memory_db: Database) -> None:
        repo = Repository(memory_db)
        project = Project(name="p")
        repo.create_project(project)

        sessions = [Session(project_id=project.id) for _ in range(3)]
        for s in sessions:
            repo.insert_session(s)

        listed = repo.list_sessions(project.id)
        assert len(listed) == 3

    def test_list_sessions_with_limit(self, memory_db: Database) -> None:
        repo = Repository(memory_db)
        project = Project(name="p")
        repo.create_project(project)

        for _ in range(5):
            repo.insert_session(Session(project_id=project.id))

        listed = repo.list_sessions(project.id, limit=2)
        assert len(listed) == 2

    def test_list_sessions_with_offset(self, memory_db: Database) -> None:
        repo = Repository(memory_db)
        project = Project(name="p")
        repo.create_project(project)

        for _ in range(5):
            repo.insert_session(Session(project_id=project.id))

        listed = repo.list_sessions(project.id, limit=2, offset=3)
        assert len(listed) == 2


class TestEventQueries:
    def _make_project_and_session(
        self, repo: Repository
    ) -> tuple[Project, Session]:
        project = Project(name="p")
        repo.create_project(project)
        session = Session(project_id=project.id)
        repo.insert_session(session)
        return project, session

    def test_insert_and_get_event(self, memory_db: Database) -> None:
        repo = Repository(memory_db)
        project, session = self._make_project_and_session(repo)
        event = Event(
            session_id=session.id,
            project_id=project.id,
            type="prompt",
            content="Hello",
        )
        repo.insert_event(event)
        fetched = repo.get_event(event.id)
        assert fetched is not None
        assert fetched.content == "Hello"
        assert fetched.type == "prompt"

    def test_get_event_not_found(self, memory_db: Database) -> None:
        repo = Repository(memory_db)
        assert repo.get_event("nonexistent") is None

    def test_insert_events_batch(self, memory_db: Database) -> None:
        repo = Repository(memory_db)
        project, session = self._make_project_and_session(repo)
        events = [
            Event(
                session_id=session.id,
                project_id=project.id,
                type="prompt",
                content=f"Event {i}",
            )
            for i in range(5)
        ]
        repo.insert_events(events)
        fetched = repo.get_events(project.id)
        assert len(fetched) == 5

    def test_get_events_by_session(self, memory_db: Database) -> None:
        repo = Repository(memory_db)
        project = Project(name="p")
        repo.create_project(project)
        s1 = Session(project_id=project.id)
        s2 = Session(project_id=project.id)
        repo.insert_session(s1)
        repo.insert_session(s2)

        repo.insert_event(
            Event(session_id=s1.id, project_id=project.id, type="prompt", content="A")
        )
        repo.insert_event(
            Event(session_id=s2.id, project_id=project.id, type="prompt", content="B")
        )

        assert len(repo.get_events(project.id, session_id=s1.id)) == 1
        assert len(repo.get_events(project.id, session_id=s2.id)) == 1

    def test_get_events_by_type(self, memory_db: Database) -> None:
        repo = Repository(memory_db)
        project, session = self._make_project_and_session(repo)
        repo.insert_event(
            Event(session_id=session.id, project_id=project.id, type="prompt", content="A")
        )
        repo.insert_event(
            Event(session_id=session.id, project_id=project.id, type="response", content="B")
        )

        prompts = repo.get_events(project.id, type="prompt")
        assert len(prompts) == 1
        assert prompts[0].type == "prompt"

    def test_count_events(self, memory_db: Database) -> None:
        repo = Repository(memory_db)
        project, session = self._make_project_and_session(repo)
        for i in range(3):
            repo.insert_event(
                Event(
                    session_id=session.id,
                    project_id=project.id,
                    type="prompt",
                    content=f"Event {i}",
                )
            )
        assert repo.count_events(project.id) == 3

    def test_count_events_by_session(self, memory_db: Database) -> None:
        repo = Repository(memory_db)
        project = Project(name="p")
        repo.create_project(project)
        s1 = Session(project_id=project.id)
        s2 = Session(project_id=project.id)
        repo.insert_session(s1)
        repo.insert_session(s2)

        for i in range(3):
            repo.insert_event(
                Event(
                    session_id=s1.id, project_id=project.id,
                    type="prompt", content=f"A{i}",
                )
            )
        repo.insert_event(
            Event(
                session_id=s2.id, project_id=project.id,
                type="prompt", content="B0",
            )
        )

        assert repo.count_events(project.id, session_id=s1.id) == 3
        assert repo.count_events(project.id, session_id=s2.id) == 1

    def test_get_newest_event_timestamp_none_when_empty(
        self, memory_db: Database,
    ) -> None:
        repo = Repository(memory_db)
        project, _ = self._make_project_and_session(repo)
        assert repo.get_newest_event_timestamp(project.id) is None

    def test_get_newest_event_timestamp_returns_latest(
        self, memory_db: Database,
    ) -> None:
        repo = Repository(memory_db)
        project, session = self._make_project_and_session(repo)
        repo.insert_event(
            Event(
                session_id=session.id, project_id=project.id,
                type="prompt", content="older",
                timestamp="2020-01-01T00:00:00+00:00",
            )
        )
        repo.insert_event(
            Event(
                session_id=session.id, project_id=project.id,
                type="prompt", content="newer",
                timestamp="2024-06-01T00:00:00+00:00",
            )
        )
        assert (
            repo.get_newest_event_timestamp(project.id)
            == "2024-06-01T00:00:00+00:00"
        )

    def test_find_recent_event_found(self, memory_db: Database) -> None:
        repo = Repository(memory_db)
        project, session = self._make_project_and_session(repo)
        repo.insert_event(
            Event(
                session_id=session.id, project_id=project.id,
                type="prompt", content="unique content",
            )
        )
        found = repo.find_recent_event(project.id, "unique content", "prompt", 60)
        assert found is not None
        assert found.content == "unique content"

    def test_find_recent_event_not_found(self, memory_db: Database) -> None:
        repo = Repository(memory_db)
        project, session = self._make_project_and_session(repo)
        repo.insert_event(
            Event(
                session_id=session.id, project_id=project.id,
                type="prompt", content="content",
            )
        )
        found = repo.find_recent_event(project.id, "different content", "prompt", 60)
        assert found is None


class TestEventFtsSanitization:
    """search_events_fts must never pass raw user input to MATCH."""

    def test_hyphenated_query_does_not_raise(self, memory_db: Database) -> None:
        repo = Repository(memory_db)
        project = Project(name="p")
        repo.create_project(project)
        session = Session(project_id=project.id)
        repo.insert_session(session)
        repo.insert_event(Event(
            session_id=session.id, project_id=project.id,
            type="note", content="cookie-backed sessions are in use",
        ))

        # A raw MATCH would raise `fts5: syntax error near "-"` here.
        # Sanitized, it must return a (possibly empty) list instead.
        results = repo.search_events_fts(project.id, "cookie-backed")
        assert isinstance(results, list)

    def test_hyphenated_query_matches_hyphenated_document(
        self, memory_db: Database
    ) -> None:
        # The FTS5 tokenizer (porter unicode61) splits "cookie-backed"
        # into separate "cookie"/"backed" tokens when indexing the
        # document. The query must be split the same way (not glued
        # into "cookiebacked") or it will never match.
        repo = Repository(memory_db)
        project = Project(name="p")
        repo.create_project(project)
        session = Session(project_id=project.id)
        repo.insert_session(session)
        repo.insert_event(Event(
            session_id=session.id, project_id=project.id,
            type="note", content="cookie-backed sessions are in use",
        ))

        results = repo.search_events_fts(project.id, "cookie-backed")
        assert any("cookie-backed" in r["content"].lower() for r in results)

    @pytest.mark.parametrize("query", HOSTILE_QUERIES)
    def test_hostile_queries_never_raise(
        self, memory_db: Database, query: str
    ) -> None:
        repo = Repository(memory_db)
        project = Project(name="p")
        repo.create_project(project)
        session = Session(project_id=project.id)
        repo.insert_session(session)
        repo.insert_event(Event(
            session_id=session.id, project_id=project.id,
            type="note", content="some note content",
        ))

        results = repo.search_events_fts(project.id, query)
        assert isinstance(results, list)


class TestEntityFtsSearch:
    """entities_fts must actually be used for entity search."""

    def test_and_then_or_fallback(self, memory_db: Database) -> None:
        repo = Repository(memory_db)
        project = Project(name="p")
        repo.create_project(project)
        repo.create_entity(Entity(
            project_id=project.id, type="fact",
            title="alpha only entity", content="content",
        ))

        # "alpha" matches, "zzznomatch" matches nothing — an AND join
        # yields zero rows, so the OR fallback must still find it.
        results = repo.search_entities_fts(project.id, "alpha zzznomatch")
        assert any(r["title"] == "alpha only entity" for r in results)

    def test_hyphenated_query_matches_hyphenated_document(
        self, memory_db: Database
    ) -> None:
        # Same compound-splitting requirement as events: the FTS5
        # tokenizer splits "cookie-backed" into separate tokens when
        # indexing, so the query must split the same way to match.
        repo = Repository(memory_db)
        project = Project(name="p")
        repo.create_project(project)
        repo.create_entity(Entity(
            project_id=project.id, type="fact",
            title="cookie-backed session handling", content="detail",
        ))

        results = repo.search_entities_fts(project.id, "cookie-backed")
        assert any(
            "cookie-backed" in r["title"].lower() for r in results
        )

    @pytest.mark.parametrize("query", HOSTILE_QUERIES)
    def test_hostile_queries_never_raise(
        self, memory_db: Database, query: str
    ) -> None:
        repo = Repository(memory_db)
        project = Project(name="p")
        repo.create_project(project)
        repo.create_entity(Entity(
            project_id=project.id, type="fact",
            title="cookie-backed session handling", content="detail",
        ))

        results = repo.search_entities_fts(project.id, query)
        assert isinstance(results, list)

    def test_excludes_archived_entities(self, memory_db: Database) -> None:
        repo = Repository(memory_db)
        project = Project(name="p")
        repo.create_project(project)
        entity = Entity(
            project_id=project.id, type="fact",
            title="archived widget details", content="content",
        )
        repo.create_entity(entity)
        conn = memory_db.connect()
        try:
            conn.execute(
                "UPDATE entities SET archived_at = datetime('now') WHERE id = ?",
                (entity.id,),
            )
            conn.commit()
        finally:
            conn.close()

        results = repo.search_entities_fts(project.id, "widget")
        assert results == []

    def test_excludes_stale_entities_by_default(self, memory_db: Database) -> None:
        repo = Repository(memory_db)
        project = Project(name="p")
        repo.create_project(project)
        entity = Entity(
            project_id=project.id, type="fact",
            title="staleflagword entity", content="content",
        )
        repo.create_entity(entity)
        repo.mark_stale(entity.id, reason="superseded")

        assert repo.search_entities_fts(project.id, "staleflagword") == []
        included = repo.search_entities_fts(
            project.id, "staleflagword", include_stale=True,
        )
        assert any(r["id"] == entity.id for r in included)


class TestFindEntitiesByShortId:
    """Collision-safe, project-scoped short-ID resolution — the helper
    every entity-id-taking MCP tool must share (mem_get_entities already
    resolved short IDs via a one-off, non-project-scoped LIMIT-1 query;
    this replaces it)."""

    def test_matches_by_suffix(self, memory_db: Database) -> None:
        repo = Repository(memory_db)
        project = Project(name="p")
        repo.create_project(project)
        entity = Entity(
            project_id=project.id, type="fact",
            title="short id target", content="content",
        )
        repo.create_entity(entity)

        matches = repo.find_entities_by_short_id(project.id, entity.id[-8:])
        assert [m["id"] for m in matches] == [entity.id]

    def test_matches_by_prefix(self, memory_db: Database) -> None:
        repo = Repository(memory_db)
        project = Project(name="p")
        repo.create_project(project)
        entity = Entity(
            project_id=project.id, type="fact",
            title="short id target", content="content",
        )
        repo.create_entity(entity)

        matches = repo.find_entities_by_short_id(project.id, entity.id[:8])
        assert [m["id"] for m in matches] == [entity.id]

    def test_no_match_returns_empty_list(self, memory_db: Database) -> None:
        repo = Repository(memory_db)
        project = Project(name="p")
        repo.create_project(project)

        assert repo.find_entities_by_short_id(project.id, "ZZZZZZZZ") == []

    def test_ambiguous_short_id_returns_every_match(
        self, memory_db: Database,
    ) -> None:
        repo = Repository(memory_db)
        project = Project(name="p")
        repo.create_project(project)
        e1 = Entity(
            id="AAAAAAAAAAAAAAAAAA01234567",
            project_id=project.id, type="fact",
            title="first collider", content="content",
        )
        e2 = Entity(
            id="BBBBBBBBBBBBBBBBBB01234567",
            project_id=project.id, type="fact",
            title="second collider", content="content",
        )
        repo.create_entity(e1)
        repo.create_entity(e2)

        matches = repo.find_entities_by_short_id(project.id, "01234567")
        assert {m["id"] for m in matches} == {e1.id, e2.id}

    def test_scoped_to_project_no_cross_project_collision(
        self, memory_db: Database,
    ) -> None:
        repo = Repository(memory_db)
        project_a = Project(name="a")
        project_b = Project(name="b")
        repo.create_project(project_a)
        repo.create_project(project_b)
        in_a = Entity(
            id="AAAAAAAAAAAAAAAAAA01234567",
            project_id=project_a.id, type="fact",
            title="lives in project a", content="content",
        )
        in_b = Entity(
            id="BBBBBBBBBBBBBBBBBB01234567",
            project_id=project_b.id, type="fact",
            title="lives in project b", content="content",
        )
        repo.create_entity(in_a)
        repo.create_entity(in_b)

        matches = repo.find_entities_by_short_id(project_a.id, "01234567")
        assert [m["id"] for m in matches] == [in_a.id]


class TestMarkResolved:
    """mark_resolved backs the mem_resolve MCP tool -- the closing verb
    that was missing, which forced agents to fall back to mem_mark_stale
    (leaving entities open+stale=1: invisible but unresolved)."""

    def test_sets_status_and_resolved_at(self, memory_db: Database) -> None:
        repo = Repository(memory_db)
        project = Project(name="p")
        repo.create_project(project)
        entity = Entity(
            project_id=project.id, type="todo", status="open",
            title="ship the thing", content="content",
        )
        repo.create_entity(entity)

        result = repo.mark_resolved(entity.id, "done")
        assert result is not None
        assert result["status"] == "done"
        assert result["old_status"] == "open"
        assert result["resolved_at"]
        assert result["unchanged"] is False

    def test_clears_stale_flag(self, memory_db: Database) -> None:
        """The exact defect scenario: an agent fell back to mark_stale as
        a workaround for the missing 'done' verb, leaving the entity
        open+stale=1. Resolving it must clear stale so the record is
        coherent again."""
        repo = Repository(memory_db)
        project = Project(name="p")
        repo.create_project(project)
        entity = Entity(
            project_id=project.id, type="todo", status="open",
            title="wrong-verb'd todo", content="content",
        )
        repo.create_entity(entity)
        repo.mark_stale(entity.id, reason="outdated")
        assert repo.get_entity(entity.id)["stale"] == 1

        result = repo.mark_resolved(entity.id, "done")
        assert result["stale"] == 0
        assert result["unchanged"] is False

    def test_unchanged_when_already_at_status_and_not_stale(
        self, memory_db: Database,
    ) -> None:
        repo = Repository(memory_db)
        project = Project(name="p")
        repo.create_project(project)
        entity = Entity(
            project_id=project.id, type="todo", status="done",
            title="already done", content="content",
        )
        repo.create_entity(entity)

        result = repo.mark_resolved(entity.id, "done")
        assert result["unchanged"] is True
        assert result["old_status"] == "done"

    def test_unknown_entity_returns_none(self, memory_db: Database) -> None:
        repo = Repository(memory_db)
        assert repo.mark_resolved("nonexistent", "done") is None

    def test_note_is_stored_in_metadata(self, memory_db: Database) -> None:
        repo = Repository(memory_db)
        project = Project(name="p")
        repo.create_project(project)
        entity = Entity(
            project_id=project.id, type="todo", status="open",
            title="ship the thing", content="content",
        )
        repo.create_entity(entity)

        result = repo.mark_resolved(entity.id, "done", note="shipped in v2")
        assert json.loads(result["metadata"])["resolution_note"] == "shipped in v2"

    def test_note_merges_into_pre_existing_metadata(
        self, memory_db: Database,
    ) -> None:
        repo = Repository(memory_db)
        project = Project(name="p")
        repo.create_project(project)
        entity = Entity(
            project_id=project.id, type="todo", status="open",
            title="ship the thing", content="content",
            metadata={"source": "auto-extraction", "confidence": 0.9},
        )
        repo.create_entity(entity)

        result = repo.mark_resolved(entity.id, "done", note="shipped in v2")
        metadata = json.loads(result["metadata"])
        assert metadata["resolution_note"] == "shipped in v2"
        # Pre-existing keys must survive the merge, not be clobbered.
        assert metadata["source"] == "auto-extraction"
        assert metadata["confidence"] == 0.9


class TestMarkReopened:
    """mark_reopened backs the mem_reopen MCP tool -- the symmetric inverse
    of mem_resolve. Live-forensics defect: an agent reopening four wrongly
    closed entities found no reopen verb, hand-rolled SQLite, and left one
    entity in a half-state (status='done', resolved_at=NULL)."""

    def test_sets_status_open_and_clears_resolved_at_for_todo(
        self, memory_db: Database,
    ) -> None:
        repo = Repository(memory_db)
        project = Project(name="p")
        repo.create_project(project)
        entity = Entity(
            project_id=project.id, type="todo", status="done",
            title="wrongly closed", content="content",
        )
        repo.create_entity(entity)
        repo.mark_resolved(entity.id, "done")

        result = repo.mark_reopened(entity.id)
        assert result is not None
        assert result["status"] == "open"
        assert result["old_status"] == "done"
        assert result["resolved_at"] is None
        assert result["unchanged"] is False

    def test_sets_status_unresolved_for_failure(
        self, memory_db: Database,
    ) -> None:
        repo = Repository(memory_db)
        project = Project(name="p")
        repo.create_project(project)
        entity = Entity(
            project_id=project.id, type="failure", status="resolved",
            title="root cause turned out wrong", content="content",
        )
        repo.create_entity(entity)

        result = repo.mark_reopened(entity.id)
        assert result["status"] == "unresolved"
        assert result["old_status"] == "resolved"

    def test_repairs_half_state_done_with_null_resolved_at(
        self, memory_db: Database,
    ) -> None:
        """The exact incident state: status='done' but resolved_at was
        never set (left by a hand-rolled SQL update). Reopening must
        still normalize this to a coherent open state."""
        repo = Repository(memory_db)
        project = Project(name="p")
        repo.create_project(project)
        entity = Entity(
            project_id=project.id, type="todo", status="done",
            title="half-closed", content="content", resolved_at=None,
        )
        repo.create_entity(entity)

        result = repo.mark_reopened(entity.id)
        assert result["status"] == "open"
        assert result["old_status"] == "done"
        assert result["resolved_at"] is None
        assert result["unchanged"] is False

    def test_unchanged_when_already_open(self, memory_db: Database) -> None:
        repo = Repository(memory_db)
        project = Project(name="p")
        repo.create_project(project)
        entity = Entity(
            project_id=project.id, type="todo", status="open",
            title="never closed", content="content",
        )
        repo.create_entity(entity)

        result = repo.mark_reopened(entity.id)
        assert result["unchanged"] is True
        assert result["old_status"] == "open"
        assert result["status"] == "open"

    def test_unknown_entity_returns_none(self, memory_db: Database) -> None:
        repo = Repository(memory_db)
        assert repo.mark_reopened("nonexistent") is None

    def test_removes_resolution_note_non_destructively(
        self, memory_db: Database,
    ) -> None:
        repo = Repository(memory_db)
        project = Project(name="p")
        repo.create_project(project)
        entity = Entity(
            project_id=project.id, type="todo", status="open",
            title="ship it", content="content",
            metadata={"source": "auto-extraction", "confidence": 0.9},
        )
        repo.create_entity(entity)
        repo.mark_resolved(entity.id, "done", note="shipped in v2")

        result = repo.mark_reopened(entity.id)
        metadata = json.loads(result["metadata"])
        assert "resolution_note" not in metadata
        # Pre-existing keys must survive removal, not be clobbered.
        assert metadata["source"] == "auto-extraction"
        assert metadata["confidence"] == 0.9

    def test_note_is_stored_in_metadata(self, memory_db: Database) -> None:
        repo = Repository(memory_db)
        project = Project(name="p")
        repo.create_project(project)
        entity = Entity(
            project_id=project.id, type="todo", status="done",
            title="ship the thing", content="content",
        )
        repo.create_entity(entity)

        result = repo.mark_reopened(entity.id, note="reopened by mistake")
        assert json.loads(result["metadata"])["resolution_note"] == "reopened by mistake"

    def test_leaves_stale_and_pinned_untouched(
        self, memory_db: Database,
    ) -> None:
        repo = Repository(memory_db)
        project = Project(name="p")
        repo.create_project(project)
        entity = Entity(
            project_id=project.id, type="todo", status="done",
            title="closed but stale/pinned", content="content",
            pinned=True,
        )
        repo.create_entity(entity)
        repo.mark_stale(entity.id, reason="manual")

        result = repo.mark_reopened(entity.id)
        assert result["stale"] == 1
        assert result["staleness_reason"] == "manual"
        assert result["pinned"] == 1

    def test_rejects_decision_type_without_mutating(
        self, memory_db: Database,
    ) -> None:
        """A decision resolved via mem_resolve (which has no type
        restriction) has no evidenced open status to restore -- reopening
        it must not fabricate one."""
        repo = Repository(memory_db)
        project = Project(name="p")
        repo.create_project(project)
        entity = Entity(
            project_id=project.id, type="decision", status="done",
            title="use postgres", content="content",
        )
        repo.create_entity(entity)

        result = repo.mark_reopened(entity.id)
        assert result["unsupported_type"] is True
        assert result["type"] == "decision"
        row = repo.get_entity(entity.id)
        assert row["status"] == "done"

    def test_rejects_fact_type_never_closed(self, memory_db: Database) -> None:
        """A fact's resting state is status=None -- it was never
        'closed', so reopening it must not invent 'unresolved'."""
        repo = Repository(memory_db)
        project = Project(name="p")
        repo.create_project(project)
        entity = Entity(
            project_id=project.id, type="fact",
            title="the API rate limit is 100rpm", content="content",
        )
        repo.create_entity(entity)
        assert entity.status is None

        result = repo.mark_reopened(entity.id)
        assert result["unsupported_type"] is True
        row = repo.get_entity(entity.id)
        assert row["status"] is None

    def test_never_closed_todo_with_null_status_is_unchanged(
        self, memory_db: Database,
    ) -> None:
        """Defense in depth: even for an allowed type, a status=None
        entity was never closed, so reopening it is a no-op rather than
        fabricating status='open'."""
        repo = Repository(memory_db)
        project = Project(name="p")
        repo.create_project(project)
        entity = Entity(
            project_id=project.id, type="todo",
            title="never touched", content="content",
        )
        repo.create_entity(entity)
        assert entity.status is None

        result = repo.mark_reopened(entity.id)
        assert result["unchanged"] is True
        assert result["old_status"] is None
        row = repo.get_entity(entity.id)
        assert row["status"] is None


class TestGetEntitiesSourceText:
    """Batched counterpart to ``get_entity_source_text`` -- fetches source
    text for MANY entities in ONE query. Feeds the judged resolve sweep's
    discussion guard and judge prompt, both of which need every driver's
    source text up front rather than one query per driver (the N+1 the
    live per-driver lazy fetch is fine for, but a sweep over dozens of
    drivers is not)."""

    @staticmethod
    def _entity_with_events(
        engine: MemoryEngine, contents: list[str],
    ) -> tuple[str, list[str]]:
        """Create real events (via MemoryEngine, so FK constraints are
        satisfied) and an entity whose source_event_ids point at them.
        Returns (entity_id, event_ids)."""
        event_ids = []
        for content in contents:
            event = engine.ingest_one("note", content)
            assert event is not None
            event_ids.append(event.id)

        repo = Repository(engine.db)
        entity = Entity(
            project_id=engine.project_id, type="feature", title="driver",
            content="driver", source_event_ids=event_ids,
        )
        repo.create_entity(entity)
        return entity.id, event_ids

    def test_matches_n_individual_fetches(self, memory_db: Database) -> None:
        from callmem.core.engine import MemoryEngine
        from callmem.models.config import Config

        config = Config(sensitive_data={"enabled": False, "llm_scan": False})
        engine = MemoryEngine(memory_db, config)
        engine.start_session()
        repo = Repository(memory_db)

        id_a, _ = self._entity_with_events(
            engine, ["fixed the thing", "more detail"],
        )
        id_b, _ = self._entity_with_events(
            engine, ["separate event text"],
        )

        individually = {
            eid: repo.get_entity_source_text(eid) for eid in (id_a, id_b)
        }
        batched = repo.get_entities_source_text([id_a, id_b])
        assert batched == individually

    def test_empty_input_returns_empty_dict(self, memory_db: Database) -> None:
        repo = Repository(memory_db)
        assert repo.get_entities_source_text([]) == {}

    def test_entity_with_no_source_events_maps_to_empty_string(
        self, memory_db: Database,
    ) -> None:
        repo = Repository(memory_db)
        project = Project(name="p")
        repo.create_project(project)
        entity = Entity(
            project_id=project.id, type="feature", title="no events",
            content="no events",
        )
        repo.create_entity(entity)

        result = repo.get_entities_source_text([entity.id])
        assert result[entity.id] == ""

    def test_unknown_id_omitted_or_empty(self, memory_db: Database) -> None:
        repo = Repository(memory_db)
        result = repo.get_entities_source_text(["does-not-exist"])
        assert result.get("does-not-exist", "") == ""


class TestTransferCitations:
    """transfer_citations backs consolidation's citation-credit transfer
    (Task 3): when consolidation archives or supersedes an entity, the
    survivor must inherit its cited_count so briefing importance ranking
    doesn't get demoted by consolidation's own cleanup."""

    def _entity(self, repo: Repository, project_id: str, title: str) -> Entity:
        entity = Entity(
            project_id=project_id, type="fact", title=title, content="c",
        )
        repo.create_entity(entity)
        return entity

    def test_adds_source_count_onto_target(self, memory_db: Database) -> None:
        repo = Repository(memory_db)
        project = Project(name="p")
        repo.create_project(project)
        source = self._entity(repo, project.id, title="dup")
        target = self._entity(repo, project.id, title="survivor")
        repo.set_citation_counts({source.id: (3, "2026-01-01T00:00:00")})
        repo.set_citation_counts({target.id: (2, "2026-01-02T00:00:00")})

        modified = repo.transfer_citations(source.id, target.id)

        assert modified is True
        assert repo.get_entity(target.id)["cited_count"] == 5

    def test_last_cited_at_carries_forward_only_when_newer(
        self, memory_db: Database,
    ) -> None:
        repo = Repository(memory_db)
        project = Project(name="p")
        repo.create_project(project)
        source = self._entity(repo, project.id, title="dup")
        target = self._entity(repo, project.id, title="survivor")
        repo.set_citation_counts({source.id: (1, "2026-01-05T00:00:00")})
        repo.set_citation_counts({target.id: (1, "2026-01-02T00:00:00")})

        repo.transfer_citations(source.id, target.id)

        assert repo.get_entity(target.id)["last_cited_at"] == "2026-01-05T00:00:00"

    def test_last_cited_at_not_regressed_when_target_already_newer(
        self, memory_db: Database,
    ) -> None:
        repo = Repository(memory_db)
        project = Project(name="p")
        repo.create_project(project)
        source = self._entity(repo, project.id, title="dup")
        target = self._entity(repo, project.id, title="survivor")
        repo.set_citation_counts({source.id: (1, "2026-01-02T00:00:00")})
        repo.set_citation_counts({target.id: (1, "2026-01-05T00:00:00")})

        repo.transfer_citations(source.id, target.id)

        assert repo.get_entity(target.id)["last_cited_at"] == "2026-01-05T00:00:00"

    def test_last_cited_at_equal_timestamps_leaves_target_unchanged(
        self, memory_db: Database,
    ) -> None:
        """Neither branch of the CASE fires when the two sides are
        exactly equal (the comparison is a strict `>`) -- the target
        keeps its own value, and the count still adds correctly."""
        repo = Repository(memory_db)
        project = Project(name="p")
        repo.create_project(project)
        source = self._entity(repo, project.id, title="dup")
        target = self._entity(repo, project.id, title="survivor")
        same_ts = "2026-01-03T00:00:00"
        repo.set_citation_counts({source.id: (1, same_ts)})
        repo.set_citation_counts({target.id: (1, same_ts)})

        repo.transfer_citations(source.id, target.id)

        assert repo.get_entity(target.id)["last_cited_at"] == same_ts
        assert repo.get_entity(target.id)["cited_count"] == 2

    def test_source_zeroed_after_transfer(self, memory_db: Database) -> None:
        repo = Repository(memory_db)
        project = Project(name="p")
        repo.create_project(project)
        source = self._entity(repo, project.id, title="dup")
        target = self._entity(repo, project.id, title="survivor")
        repo.set_citation_counts({source.id: (4, "2026-01-01T00:00:00")})

        repo.transfer_citations(source.id, target.id)

        assert repo.get_entity(source.id)["cited_count"] == 0

    def test_repeat_call_is_idempotent(self, memory_db: Database) -> None:
        """The exact 'processed twice' scenario the task calls out: a
        second transfer_citations call for the same pair must not
        double-count, because the source's own count was already zeroed
        by the first call."""
        repo = Repository(memory_db)
        project = Project(name="p")
        repo.create_project(project)
        source = self._entity(repo, project.id, title="dup")
        target = self._entity(repo, project.id, title="survivor")
        repo.set_citation_counts({source.id: (4, "2026-01-01T00:00:00")})

        repo.transfer_citations(source.id, target.id)
        repo.transfer_citations(source.id, target.id)

        assert repo.get_entity(target.id)["cited_count"] == 4

    def test_zero_citation_source_is_a_noop(self, memory_db: Database) -> None:
        repo = Repository(memory_db)
        project = Project(name="p")
        repo.create_project(project)
        source = self._entity(repo, project.id, title="dup")
        target = self._entity(repo, project.id, title="survivor")
        repo.set_citation_counts({target.id: (2, "2026-01-01T00:00:00")})

        modified = repo.transfer_citations(source.id, target.id)

        assert modified is True
        assert repo.get_entity(target.id)["cited_count"] == 2
        assert repo.get_entity(target.id)["last_cited_at"] == "2026-01-01T00:00:00"

    def test_same_id_source_and_target_is_a_noop(self, memory_db: Database) -> None:
        repo = Repository(memory_db)
        project = Project(name="p")
        repo.create_project(project)
        entity = self._entity(repo, project.id, title="solo")
        repo.set_citation_counts({entity.id: (3, "2026-01-01T00:00:00")})

        modified = repo.transfer_citations(entity.id, entity.id)

        assert modified is False
        assert repo.get_entity(entity.id)["cited_count"] == 3


class TestArchiveEntitySupersededBy:
    """archive_entity's optional ``superseded_by`` param (added alongside
    the citation-stranding fix): consolidation's NOOP verdict passes the
    survivor id so a citation landing on the archived duplicate after
    the fact still has a link to follow. The param must default to no
    -op for every other caller -- nothing else may start writing
    superseded_by just because this parameter exists."""

    def _entity(self, repo: Repository, project_id: str, title: str) -> Entity:
        entity = Entity(
            project_id=project_id, type="fact", title=title, content="c",
        )
        repo.create_entity(entity)
        return entity

    def test_default_call_leaves_superseded_by_unset(
        self, memory_db: Database,
    ) -> None:
        repo = Repository(memory_db)
        project = Project(name="p")
        repo.create_project(project)
        entity = self._entity(repo, project.id, title="orphan")

        modified = repo.archive_entity(entity.id)

        assert modified is True
        row = repo.get_entity(entity.id)
        assert row["archived_at"] is not None
        assert row["superseded_by"] is None

    def test_superseded_by_param_records_the_survivor(
        self, memory_db: Database,
    ) -> None:
        repo = Repository(memory_db)
        project = Project(name="p")
        repo.create_project(project)
        duplicate = self._entity(repo, project.id, title="dup")
        survivor = self._entity(repo, project.id, title="survivor")

        modified = repo.archive_entity(duplicate.id, superseded_by=survivor.id)

        assert modified is True
        row = repo.get_entity(duplicate.id)
        assert row["archived_at"] is not None
        assert row["superseded_by"] == survivor.id

    def test_already_archived_entity_guard_still_applies(
        self, memory_db: Database,
    ) -> None:
        """The ``WHERE archived_at IS NULL`` guard is unchanged by the
        new parameter -- a second archive attempt (with or without
        superseded_by) is still a checked no-op."""
        repo = Repository(memory_db)
        project = Project(name="p")
        repo.create_project(project)
        entity = self._entity(repo, project.id, title="dup")
        survivor = self._entity(repo, project.id, title="survivor")
        repo.archive_entity(entity.id)

        modified = repo.archive_entity(entity.id, superseded_by=survivor.id)

        assert modified is False
        assert repo.get_entity(entity.id)["superseded_by"] is None


class TestIncrementCitationCountsRedirectsThroughSupersession:
    """increment_citation_counts backs the session-end citation hook. A
    session can end after consolidation has already archived/superseded
    the entity a citation names -- without redirecting through
    superseded_by to the live survivor, that credit is permanently
    stranded on a dead row briefing ranking never looks at again."""

    def _entity(self, repo: Repository, project_id: str, title: str) -> Entity:
        entity = Entity(
            project_id=project_id, type="fact", title=title, content="c",
        )
        repo.create_entity(entity)
        return entity

    def test_live_entity_is_credited_directly(self, memory_db: Database) -> None:
        repo = Repository(memory_db)
        project = Project(name="p")
        repo.create_project(project)
        entity = self._entity(repo, project.id, title="live")

        updated = repo.increment_citation_counts(
            {entity.id: (2, "2026-01-01T00:00:00")},
        )

        assert updated == 1
        assert repo.get_entity(entity.id)["cited_count"] == 2

    def test_archived_entity_with_superseded_by_credits_the_survivor(
        self, memory_db: Database,
    ) -> None:
        repo = Repository(memory_db)
        project = Project(name="p")
        repo.create_project(project)
        old = self._entity(repo, project.id, title="old")
        new = self._entity(repo, project.id, title="new")
        repo.archive_entity(old.id)
        repo.mark_stale(old.id, reason="consolidated", superseded_by=new.id)

        repo.increment_citation_counts({old.id: (3, "2026-01-05T00:00:00")})

        assert repo.get_entity(new.id)["cited_count"] == 3
        assert repo.get_entity(old.id)["cited_count"] == 0

    def test_stale_not_archived_entity_with_superseded_by_also_redirects(
        self, memory_db: Database,
    ) -> None:
        """The redirect condition is archived OR stale, not just
        archived -- UPDATE/CONTRADICTS supersede without archiving."""
        repo = Repository(memory_db)
        project = Project(name="p")
        repo.create_project(project)
        old = self._entity(repo, project.id, title="old")
        new = self._entity(repo, project.id, title="new")
        repo.mark_stale(old.id, reason="consolidated", superseded_by=new.id)
        assert repo.get_entity(old.id)["archived_at"] is None  # stale, not archived

        repo.increment_citation_counts({old.id: (1, "2026-01-05T00:00:00")})

        assert repo.get_entity(new.id)["cited_count"] == 1
        assert repo.get_entity(old.id)["cited_count"] == 0

    def test_two_hop_chain_credits_the_final_live_survivor(
        self, memory_db: Database,
    ) -> None:
        repo = Repository(memory_db)
        project = Project(name="p")
        repo.create_project(project)
        oldest = self._entity(repo, project.id, title="oldest")
        middle = self._entity(repo, project.id, title="middle")
        newest = self._entity(repo, project.id, title="newest")
        repo.mark_stale(oldest.id, reason="consolidated", superseded_by=middle.id)
        repo.mark_stale(middle.id, reason="consolidated", superseded_by=newest.id)

        repo.increment_citation_counts({oldest.id: (4, "2026-01-05T00:00:00")})

        assert repo.get_entity(newest.id)["cited_count"] == 4
        assert repo.get_entity(middle.id)["cited_count"] == 0
        assert repo.get_entity(oldest.id)["cited_count"] == 0

    def test_archived_with_no_superseded_by_credits_dead_row_and_logs_debug(
        self, memory_db: Database, caplog: pytest.LogCaptureFixture,
    ) -> None:
        repo = Repository(memory_db)
        project = Project(name="p")
        repo.create_project(project)
        orphan = self._entity(repo, project.id, title="orphan")
        repo.archive_entity(orphan.id)

        with caplog.at_level(logging.DEBUG, logger="callmem.core.repository"):
            updated = repo.increment_citation_counts(
                {orphan.id: (1, "2026-01-01T00:00:00")},
            )

        # Today's behaviour: credit lands on the dead row, no crash.
        assert updated == 1
        assert repo.get_entity(orphan.id)["cited_count"] == 1
        assert any("no live successor" in r.message for r in caplog.records)

    def test_self_referencing_superseded_by_does_not_infinite_loop(
        self, memory_db: Database,
    ) -> None:
        """Cycles are structurally impossible via mark_stale (an entity
        can't name itself as its own supersessor through normal calls),
        but the walk must not rely on that -- construct one directly via
        raw SQL and confirm the bounded walk still terminates safely."""
        repo = Repository(memory_db)
        project = Project(name="p")
        repo.create_project(project)
        entity = self._entity(repo, project.id, title="cycle")
        conn = memory_db.connect()
        try:
            conn.execute(
                "UPDATE entities SET stale = 1, superseded_by = ? WHERE id = ?",
                (entity.id, entity.id),
            )
            conn.commit()
        finally:
            conn.close()

        updated = repo.increment_citation_counts(
            {entity.id: (1, "2026-01-01T00:00:00")},
        )

        assert updated == 1
        assert repo.get_entity(entity.id)["cited_count"] == 1

    def test_two_hop_cycle_does_not_infinite_loop(
        self, memory_db: Database,
    ) -> None:
        repo = Repository(memory_db)
        project = Project(name="p")
        repo.create_project(project)
        a = self._entity(repo, project.id, title="a")
        b = self._entity(repo, project.id, title="b")
        conn = memory_db.connect()
        try:
            conn.execute(
                "UPDATE entities SET stale = 1, superseded_by = ? WHERE id = ?",
                (b.id, a.id),
            )
            conn.execute(
                "UPDATE entities SET stale = 1, superseded_by = ? WHERE id = ?",
                (a.id, b.id),
            )
            conn.commit()
        finally:
            conn.close()

        # Must terminate (the hop bound guards this even though the
        # cycle guard should catch it first) and must not crash.
        updated = repo.increment_citation_counts(
            {a.id: (1, "2026-01-01T00:00:00")},
        )
        assert updated == 1

    def test_chain_longer_than_hop_bound_terminates(
        self, memory_db: Database,
    ) -> None:
        repo = Repository(memory_db)
        project = Project(name="p")
        repo.create_project(project)
        chain = [self._entity(repo, project.id, title=f"n{i}") for i in range(15)]
        conn = memory_db.connect()
        try:
            for older, newer in zip(chain, chain[1:], strict=False):
                conn.execute(
                    "UPDATE entities SET stale = 1, superseded_by = ? "
                    "WHERE id = ?",
                    (newer.id, older.id),
                )
            conn.commit()
        finally:
            conn.close()

        # Must terminate within the hop bound rather than walking all 14
        # hops to the true end of the chain.
        updated = repo.increment_citation_counts(
            {chain[0].id: (1, "2026-01-01T00:00:00")},
        )
        assert updated == 1
        assert repo.get_entity(chain[-1].id)["cited_count"] == 0

    def test_two_sources_resolving_to_the_same_survivor_are_summed(
        self, memory_db: Database,
    ) -> None:
        repo = Repository(memory_db)
        project = Project(name="p")
        repo.create_project(project)
        old_a = self._entity(repo, project.id, title="old-a")
        old_b = self._entity(repo, project.id, title="old-b")
        survivor = self._entity(repo, project.id, title="survivor")
        repo.mark_stale(old_a.id, reason="consolidated", superseded_by=survivor.id)
        repo.mark_stale(old_b.id, reason="consolidated", superseded_by=survivor.id)

        repo.increment_citation_counts({
            old_a.id: (2, "2026-01-01T00:00:00"),
            old_b.id: (3, "2026-01-03T00:00:00"),
        })

        row = repo.get_entity(survivor.id)
        assert row["cited_count"] == 5
        assert row["last_cited_at"] == "2026-01-03T00:00:00"

    def test_none_last_cited_at_does_not_write_empty_string(
        self, memory_db: Database,
    ) -> None:
        """Not reachable through any caller today (nothing passes a None
        timestamp), but the merge must stay correct if one ever does:
        substituting "" for a missing last_cited_at would compare as
        newer than any real timestamp and get persisted as a literal
        empty string where a NULL belongs."""
        repo = Repository(memory_db)
        project = Project(name="p")
        repo.create_project(project)
        entity = self._entity(repo, project.id, title="entity")

        repo.increment_citation_counts({entity.id: (1, None)})  # type: ignore[dict-item]

        row = repo.get_entity(entity.id)
        assert row["cited_count"] == 1
        assert row["last_cited_at"] is None

    def test_missing_superseded_by_target_credits_last_real_entity(
        self, memory_db: Database,
    ) -> None:
        """A superseded_by link pointing at a row that no longer exists
        (defensive-only -- not reachable through normal mutation paths)
        must still resolve to a real id, not a phantom one that would
        silently swallow the credit."""
        repo = Repository(memory_db)
        project = Project(name="p")
        repo.create_project(project)
        entity = self._entity(repo, project.id, title="dangling")
        conn = memory_db.connect()
        try:
            conn.execute(
                "UPDATE entities SET stale = 1, superseded_by = ? WHERE id = ?",
                ("does-not-exist", entity.id),
            )
            conn.commit()
        finally:
            conn.close()

        updated = repo.increment_citation_counts(
            {entity.id: (1, "2026-01-01T00:00:00")},
        )

        assert updated == 1
        assert repo.get_entity(entity.id)["cited_count"] == 1


class TestUnarchiveProtected:
    """find_archived_protected_candidates / restore_archived_protected back
    `callmem unarchive-protected` -- a repair path for entities the
    compaction status-vocabulary bug wrongly archived (e.g. 'unresolved'
    failures) before it was fixed. Must never touch entities archived for
    legitimate reasons: closed lifecycle status, no lifecycle at all
    (status IS NULL), or staleness/dedupe archival (staleness_reason or
    superseded_by set).
    """

    def _make_project(self, memory_db: Database) -> str:
        repo = Repository(memory_db)
        project = Project(name="p")
        repo.create_project(project)
        return project.id

    def test_restores_unresolved_failure_archived_by_policy(
        self, memory_db: Database,
    ) -> None:
        repo = Repository(memory_db)
        project_id = self._make_project(memory_db)
        entity = Entity(
            project_id=project_id, type="failure", status="unresolved",
            title="open failure", content="c",
            archived_at="2026-06-01T00:00:00+00:00",
        )
        repo.create_entity(entity)

        candidates = repo.find_archived_protected_candidates(project_id)
        assert [c["id"] for c in candidates] == [entity.id]

        restored = repo.restore_archived_protected(project_id)
        assert restored == 1

        conn = memory_db.connect()
        try:
            row = conn.execute(
                "SELECT archived_at FROM entities WHERE id = ?", (entity.id,)
            ).fetchone()
        finally:
            conn.close()
        assert row["archived_at"] is None

    def test_does_not_restore_done_todo(self, memory_db: Database) -> None:
        repo = Repository(memory_db)
        project_id = self._make_project(memory_db)
        entity = Entity(
            project_id=project_id, type="todo", status="done",
            title="finished", content="c",
            archived_at="2026-06-01T00:00:00+00:00",
        )
        repo.create_entity(entity)

        candidates = repo.find_archived_protected_candidates(project_id)
        assert candidates == []
        assert repo.restore_archived_protected(project_id) == 0

    def test_does_not_restore_staleness_archived_entity(
        self, memory_db: Database,
    ) -> None:
        repo = Repository(memory_db)
        project_id = self._make_project(memory_db)
        entity = Entity(
            project_id=project_id, type="failure", status="unresolved",
            title="superseded failure", content="c",
            archived_at="2026-06-01T00:00:00+00:00",
        )
        repo.create_entity(entity)
        repo.mark_stale(entity.id, "superseded")

        candidates = repo.find_archived_protected_candidates(project_id)
        assert candidates == []

    def test_does_not_restore_superseded_by_set_entity(
        self, memory_db: Database,
    ) -> None:
        repo = Repository(memory_db)
        project_id = self._make_project(memory_db)
        entity = Entity(
            project_id=project_id, type="failure", status="unresolved",
            title="replaced failure", content="c",
            archived_at="2026-06-01T00:00:00+00:00",
        )
        repo.create_entity(entity)
        repo.mark_stale(entity.id, "duplicate", superseded_by="other-entity-id")

        candidates = repo.find_archived_protected_candidates(project_id)
        assert candidates == []

    def test_does_not_restore_status_null_fact(
        self, memory_db: Database,
    ) -> None:
        repo = Repository(memory_db)
        project_id = self._make_project(memory_db)
        entity = Entity(
            project_id=project_id, type="fact", status=None,
            title="a fact", content="c",
            archived_at="2026-06-01T00:00:00+00:00",
        )
        repo.create_entity(entity)

        candidates = repo.find_archived_protected_candidates(project_id)
        assert candidates == []

    def test_since_filters_to_archival_window(
        self, memory_db: Database,
    ) -> None:
        repo = Repository(memory_db)
        project_id = self._make_project(memory_db)
        old = Entity(
            project_id=project_id, type="failure", status="unresolved",
            title="archived long ago", content="c",
            archived_at="2026-01-01T00:00:00+00:00",
        )
        recent = Entity(
            project_id=project_id, type="failure", status="unresolved",
            title="archived recently", content="c",
            archived_at="2026-06-15T00:00:00+00:00",
        )
        repo.create_entity(old)
        repo.create_entity(recent)

        candidates = repo.find_archived_protected_candidates(
            project_id, since="2026-06-01"
        )
        assert [c["id"] for c in candidates] == [recent.id]

    def test_find_candidates_does_not_mutate_db(
        self, memory_db: Database,
    ) -> None:
        """The dry-run listing path must never write."""
        repo = Repository(memory_db)
        project_id = self._make_project(memory_db)
        entity = Entity(
            project_id=project_id, type="failure", status="unresolved",
            title="open failure", content="c",
            archived_at="2026-06-01T00:00:00+00:00",
        )
        repo.create_entity(entity)

        repo.find_archived_protected_candidates(project_id)

        conn = memory_db.connect()
        try:
            row = conn.execute(
                "SELECT archived_at FROM entities WHERE id = ?", (entity.id,)
            ).fetchone()
        finally:
            conn.close()
        assert row["archived_at"] == "2026-06-01T00:00:00+00:00"


def _lines_hardcoding_all_closed_statuses(src_root: Path) -> list[str]:
    """Every source line under `src_root` that quotes exactly the three
    closed lifecycle status literals ('done', 'cancelled', 'resolved')
    together -- not the full 5-value EntityStatus literal (which also
    includes 'open'/'unresolved' and is a legitimately separate concept:
    the full status vocabulary, not the closed subset). A hit outside
    CLOSED_ENTITY_STATUSES's own definition means someone re-hardcoded the
    closed-status vocabulary instead of importing the shared constant.
    """
    literal_re = re.compile(
        r"""['"](open|done|cancelled|unresolved|resolved)['"]"""
    )
    closed = {"done", "cancelled", "resolved"}
    hits: list[str] = []
    for path in src_root.rglob("*.py"):
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            found = set(literal_re.findall(line))
            if found == closed:
                hits.append(f"{path}:{lineno}: {line.strip()}")
    return hits


class TestClosedStatusVocabularySingleSource:
    """Regression guard for the status-vocabulary drift bug: the closed
    lifecycle statuses were independently hardcoded in compaction.py,
    repository.py's archive_entities_with_full_coverage, and
    mcp/tools.py's _RESOLVE_STATUSES before being consolidated into
    CLOSED_ENTITY_STATUSES. If a future change reintroduces a hardcoded
    copy anywhere under src/, this must fail instead of drifting silently.
    """

    def test_closed_status_literals_appear_only_in_the_shared_constant(
        self,
    ) -> None:
        src_root = Path(__file__).resolve().parents[2] / "src"
        hits = _lines_hardcoding_all_closed_statuses(src_root)
        assert len(hits) == 1, (
            "Expected exactly one place in src/ to hardcode the closed "
            "status literals together (CLOSED_ENTITY_STATUSES's own "
            f"definition). Found: {hits}"
        )
        assert "CLOSED_ENTITY_STATUSES" in hits[0]
