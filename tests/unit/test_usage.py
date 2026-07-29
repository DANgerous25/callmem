"""Tests for citation detection persistence (usage.py -> entities table)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from callmem.core.repository import Repository
from callmem.core.usage import (
    backfill_citation_counts,
    compute_entity_citations,
    compute_session_citations,
)
from callmem.models.entities import Entity
from callmem.models.events import Event
from callmem.models.projects import Project
from callmem.models.sessions import Session

if TYPE_CHECKING:
    from callmem.core.database import Database


def _seed_project_and_session(db: Database) -> tuple[Repository, str, str]:
    repo = Repository(db)
    project = Project(name="cited-project")
    repo.create_project(project)
    session = Session(project_id=project.id)
    repo.insert_session(session)
    return repo, project.id, session.id


def _response_event(session_id: str, project_id: str, content: str) -> Event:
    return Event(
        session_id=session_id,
        project_id=project_id,
        type="response",
        content=content,
    )


class TestComputeEntityCitations:
    def test_counts_citation_and_records_timestamp(self, tmp_db: Database) -> None:
        repo, project_id, session_id = _seed_project_and_session(tmp_db)
        decision = Entity(
            project_id=project_id, type="decision",
            title="Use SQLite", content="Chose SQLite for storage",
        )
        repo.create_entity(decision)
        repo.insert_event(_response_event(
            session_id, project_id,
            f"As decided in #{decision.id[-8:]}, we use SQLite.",
        ))

        citations = compute_entity_citations(tmp_db.db_path)

        assert citations[decision.id][0] == 1
        assert citations[decision.id][1]  # a timestamp was recorded

    def test_uncited_entity_absent_from_result(self, tmp_db: Database) -> None:
        repo, project_id, session_id = _seed_project_and_session(tmp_db)
        other = Entity(
            project_id=project_id, type="fact",
            title="Unrelated fact", content="never mentioned in a response",
        )
        repo.create_entity(other)

        citations = compute_entity_citations(tmp_db.db_path)

        assert other.id not in citations

    def test_multiple_citations_accumulate(self, tmp_db: Database) -> None:
        repo, project_id, session_id = _seed_project_and_session(tmp_db)
        decision = Entity(
            project_id=project_id, type="decision",
            title="Use SQLite", content="Chose SQLite for storage",
        )
        repo.create_entity(decision)
        short = decision.id[-8:]
        repo.insert_event(_response_event(
            session_id, project_id, f"Per #{short}, using SQLite.",
        ))
        repo.insert_event(_response_event(
            session_id, project_id, f"Reconfirming #{short} still holds.",
        ))

        citations = compute_entity_citations(tmp_db.db_path)

        assert citations[decision.id][0] == 2

    def test_lookalike_short_id_not_counted(self, tmp_db: Database) -> None:
        """A #XXXXXXXX-shaped string that doesn't match any real entity's
        short ID must not be counted — filters placeholders/session refs."""
        repo, project_id, session_id = _seed_project_and_session(tmp_db)
        decision = Entity(
            project_id=project_id, type="decision",
            title="Use SQLite", content="Chose SQLite for storage",
        )
        repo.create_entity(decision)
        repo.insert_event(_response_event(
            session_id, project_id, "See #NOTAREAL for details.",
        ))

        citations = compute_entity_citations(tmp_db.db_path)

        assert decision.id not in citations


class TestBackfillCitationCounts:
    def test_persists_cited_count_and_last_cited_at(self, tmp_db: Database) -> None:
        repo, project_id, session_id = _seed_project_and_session(tmp_db)
        decision = Entity(
            project_id=project_id, type="decision",
            title="Use SQLite", content="Chose SQLite for storage",
        )
        repo.create_entity(decision)
        repo.insert_event(_response_event(
            session_id, project_id, f"Per #{decision.id[-8:]}, using SQLite.",
        ))

        updated = backfill_citation_counts(tmp_db.db_path)

        assert updated == 1
        stored = repo.get_entity(decision.id)
        assert stored is not None
        assert stored["cited_count"] == 1
        assert stored["last_cited_at"]

    def test_is_idempotent_across_repeated_runs(self, tmp_db: Database) -> None:
        repo, project_id, session_id = _seed_project_and_session(tmp_db)
        decision = Entity(
            project_id=project_id, type="decision",
            title="Use SQLite", content="Chose SQLite for storage",
        )
        repo.create_entity(decision)
        repo.insert_event(_response_event(
            session_id, project_id, f"Per #{decision.id[-8:]}, using SQLite.",
        ))

        backfill_citation_counts(tmp_db.db_path)
        backfill_citation_counts(tmp_db.db_path)

        stored = repo.get_entity(decision.id)
        assert stored is not None
        assert stored["cited_count"] == 1

    def test_no_citations_is_a_cheap_noop(self, tmp_db: Database) -> None:
        repo, project_id, session_id = _seed_project_and_session(tmp_db)
        entity = Entity(
            project_id=project_id, type="fact",
            title="Unrelated", content="never cited",
        )
        repo.create_entity(entity)

        updated = backfill_citation_counts(tmp_db.db_path)

        assert updated == 0
        stored = repo.get_entity(entity.id)
        assert stored is not None
        assert stored["cited_count"] == 0
        assert stored["last_cited_at"] is None


class TestComputeSessionCitationsProjectScoping:
    """Session-scoped citation persistence runs on every session end, so
    it must not do an unscoped whole-database entity scan — that's an
    O(every entity in every project sharing this db file) cost on a hot
    path, and it risks a citation in one project's session getting
    attributed to a different project's entity that happens to share the
    same 8-char short-ID suffix."""

    def test_cross_project_short_id_collision_does_not_leak(
        self, tmp_db: Database,
    ) -> None:
        repo = Repository(tmp_db)
        project_a = Project(name="project-a")
        repo.create_project(project_a)
        project_b = Project(name="project-b")
        repo.create_project(project_b)

        session_a = Session(project_id=project_a.id)
        repo.insert_session(session_a)

        # Both entities share the same 8-char short-ID suffix (the part
        # actually matched against a citation) but belong to different
        # projects — a deliberately engineered collision.
        entity_a = Entity(
            id="AAAAAAAAAAAAAAAAAAAA11111111",
            project_id=project_a.id, type="decision",
            title="Project A decision", content="belongs to project A",
        )
        repo.create_entity(entity_a)
        entity_b = Entity(
            id="BBBBBBBBBBBBBBBBBBBB11111111",
            project_id=project_b.id, type="decision",
            title="Project B decision", content="belongs to project B",
        )
        repo.create_entity(entity_b)

        repo.insert_event(_response_event(
            session_a.id, project_a.id,
            "Per #11111111, going with project A's approach.",
        ))

        citations = compute_session_citations(tmp_db, session_a.id)

        assert entity_a.id in citations
        assert entity_b.id not in citations

    def test_unknown_session_id_returns_empty(self, tmp_db: Database) -> None:
        assert compute_session_citations(tmp_db, "nonexistent-session") == {}
