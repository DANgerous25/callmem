"""Tests for citation detection persistence (usage.py -> entities table)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from callmem.core.repository import Repository
from callmem.core.usage import backfill_citation_counts, compute_entity_citations
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
