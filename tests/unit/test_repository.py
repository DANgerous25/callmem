"""Tests for the Repository data access layer."""

from __future__ import annotations

from typing import TYPE_CHECKING

from llm_mem.core.repository import Repository
from llm_mem.models.events import Event
from llm_mem.models.projects import Project
from llm_mem.models.sessions import Session

if TYPE_CHECKING:
    from llm_mem.core.database import Database


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
