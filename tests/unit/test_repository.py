"""Tests for the Repository data access layer."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from callmem.core.repository import Repository
from callmem.models.entities import Entity
from callmem.models.events import Event
from callmem.models.projects import Project
from callmem.models.sessions import Session

if TYPE_CHECKING:
    from callmem.core.database import Database


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
