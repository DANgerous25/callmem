"""Tests for the judged resolve-sweep verifier (CONFIRMED/CONTRADICTED/UNCERTAIN).

Mirrors ``test_consolidation.py``'s stub-judge conventions: a candidate
match found by the keyword recall stage only closes when the judge
confirms it with evidence. Fail-open (malformed/absent judge output)
must never close anything.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from callmem.core.engine import MemoryEngine
from callmem.core.repository import Repository
from callmem.core.resolve_judge import ResolutionCandidate, ResolutionJudge
from callmem.models.config import Config
from callmem.models.entities import Entity

if TYPE_CHECKING:
    from callmem.core.database import Database


class _StubJudge:
    """Deterministic judge stand-in: records every prompt it is asked."""

    def __init__(self, response: str | None) -> None:
        self.response = response
        self.calls: list[str] = []

    def extract(self, prompt: str) -> str | None:
        self.calls.append(prompt)
        return self.response


class _SequencedJudge:
    """Returns a different canned response for each successive call --
    used to test chunking, where each chunk gets its own LLM call."""

    def __init__(self, responses: list[str | None]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    def extract(self, prompt: str) -> str | None:
        self.calls.append(prompt)
        return self.responses[len(self.calls) - 1]


def _make_engine(memory_db: Database) -> MemoryEngine:
    config = Config(sensitive_data={"enabled": False, "llm_scan": False})
    return MemoryEngine(memory_db, config)


def _insert_todo(repo: Repository, project_id: str, title: str) -> str:
    entity = Entity(
        project_id=project_id, type="todo", status="open",
        title=title, content=title,
    )
    repo.create_entity(entity)
    return entity.id


def _insert_driver(repo: Repository, project_id: str, title: str) -> str:
    entity = Entity(
        project_id=project_id, type="feature",
        title=title, content=title,
    )
    repo.create_entity(entity)
    return entity.id


def _candidate(
    driver_id: str, target_id: str,
    driver_title: str = "Implemented the selector",
    target_title: str = "Implement the selector",
    target_type: str = "todo",
) -> ResolutionCandidate:
    return ResolutionCandidate(
        driver_id=driver_id,
        driver_title=driver_title,
        driver_content="Finished wiring up the selector component.",
        driver_source_text="Finished wiring up the selector component.",
        target_id=target_id,
        target_type=target_type,
        target_title=target_title,
        target_content="Add the selector to the analysis view.",
    )


def _verdict_response(pairs_and_verdicts: list[tuple[int, str]]) -> str:
    return json.dumps([
        {"pair": pair, "verdict": verdict, "reason": "because"}
        for pair, verdict in pairs_and_verdicts
    ])


class TestConfirmedCloses:
    def test_confirmed_verdict_closes_via_mark_resolved(
        self, memory_db: Database,
    ) -> None:
        engine = _make_engine(memory_db)
        repo = Repository(memory_db)
        todo_id = _insert_todo(repo, engine.project_id, "Implement the selector")
        driver_id = _insert_driver(
            repo, engine.project_id, "Implemented the selector",
        )
        candidate = _candidate(driver_id, todo_id)

        judge = ResolutionJudge(
            memory_db, _StubJudge(_verdict_response([(1, "CONFIRMED")])),
        )
        records, stats = judge.run([candidate])

        assert stats.confirmed == 1
        assert stats.contradicted == 0
        assert stats.uncertain == 0
        assert records[0]["verdict"] == "CONFIRMED"
        assert records[0]["status"] == "done"

        row = repo.get_entity(todo_id)
        assert row["status"] == "done"

    def test_confirmed_verdict_for_failure_closes_as_resolved(
        self, memory_db: Database,
    ) -> None:
        engine = _make_engine(memory_db)
        repo = Repository(memory_db)
        failure = Entity(
            project_id=engine.project_id, type="failure", status="unresolved",
            title="Import crashes on large files", content="stack trace",
        )
        repo.create_entity(failure)
        driver_id = _insert_driver(repo, engine.project_id, "Fixed the crash")
        candidate = _candidate(
            driver_id, failure.id, target_title="Import crashes on large files",
            target_type="failure",
        )

        judge = ResolutionJudge(
            memory_db, _StubJudge(_verdict_response([(1, "CONFIRMED")])),
        )
        records, stats = judge.run([candidate])

        assert stats.confirmed == 1
        assert records[0]["status"] == "resolved"
        row = repo.get_entity(failure.id)
        assert row["status"] == "resolved"


class TestContradictedStaysOpen:
    def test_contradicted_verdict_leaves_target_open(
        self, memory_db: Database,
    ) -> None:
        engine = _make_engine(memory_db)
        repo = Repository(memory_db)
        todo_id = _insert_todo(repo, engine.project_id, "Implement the selector")
        driver_id = _insert_driver(
            repo, engine.project_id, "Implemented the selector",
        )
        candidate = _candidate(driver_id, todo_id)

        judge = ResolutionJudge(
            memory_db, _StubJudge(_verdict_response([(1, "CONTRADICTED")])),
        )
        records, stats = judge.run([candidate])

        assert stats.confirmed == 0
        assert stats.contradicted == 1
        assert stats.uncertain == 0
        assert records[0]["verdict"] == "CONTRADICTED"

        row = repo.get_entity(todo_id)
        assert row["status"] == "open"


class TestUncertainStaysOpen:
    def test_uncertain_verdict_leaves_target_open(
        self, memory_db: Database,
    ) -> None:
        engine = _make_engine(memory_db)
        repo = Repository(memory_db)
        todo_id = _insert_todo(repo, engine.project_id, "Implement the selector")
        driver_id = _insert_driver(
            repo, engine.project_id, "Implemented the selector",
        )
        candidate = _candidate(driver_id, todo_id)

        judge = ResolutionJudge(
            memory_db, _StubJudge(_verdict_response([(1, "UNCERTAIN")])),
        )
        records, stats = judge.run([candidate])

        assert stats.confirmed == 0
        assert stats.contradicted == 0
        assert stats.uncertain == 1
        assert records[0]["verdict"] == "UNCERTAIN"

        row = repo.get_entity(todo_id)
        assert row["status"] == "open"


class TestFailOpen:
    def test_malformed_response_treats_whole_chunk_as_uncertain(
        self, memory_db: Database, caplog,
    ) -> None:
        engine = _make_engine(memory_db)
        repo = Repository(memory_db)
        todo_id = _insert_todo(repo, engine.project_id, "Implement the selector")
        driver_id = _insert_driver(
            repo, engine.project_id, "Implemented the selector",
        )
        candidate = _candidate(driver_id, todo_id)

        judge = ResolutionJudge(
            memory_db, _StubJudge("not valid json at all"),
        )
        with caplog.at_level(logging.WARNING):
            records, stats = judge.run([candidate])

        assert stats.confirmed == 0
        assert stats.uncertain == 1
        assert stats.judge_failed is True
        assert records[0]["verdict"] == "UNCERTAIN"
        assert any(
            "malformed" in r.message.lower() or "fail-open" in r.message.lower()
            for r in caplog.records
        )

        row = repo.get_entity(todo_id)
        assert row["status"] == "open"

    def test_absent_response_treats_whole_chunk_as_uncertain(
        self, memory_db: Database,
    ) -> None:
        engine = _make_engine(memory_db)
        repo = Repository(memory_db)
        todo_id = _insert_todo(repo, engine.project_id, "Implement the selector")
        driver_id = _insert_driver(
            repo, engine.project_id, "Implemented the selector",
        )
        candidate = _candidate(driver_id, todo_id)

        judge = ResolutionJudge(memory_db, _StubJudge(None))
        records, stats = judge.run([candidate])

        assert stats.uncertain == 1
        assert stats.judge_failed is True
        row = repo.get_entity(todo_id)
        assert row["status"] == "open"

    def test_partial_response_missing_a_pair_fails_open(
        self, memory_db: Database,
    ) -> None:
        engine = _make_engine(memory_db)
        repo = Repository(memory_db)
        todo_a = _insert_todo(repo, engine.project_id, "Implement selector A")
        todo_b = _insert_todo(repo, engine.project_id, "Implement selector B")
        driver_id = _insert_driver(repo, engine.project_id, "Implemented A and B")
        candidates = [
            _candidate(driver_id, todo_a, target_title="Implement selector A"),
            _candidate(driver_id, todo_b, target_title="Implement selector B"),
        ]

        # Only answers pair 1 -- pair 2 is missing entirely.
        judge = ResolutionJudge(
            memory_db, _StubJudge(_verdict_response([(1, "CONFIRMED")])),
        )
        records, stats = judge.run(candidates)

        assert stats.judge_failed is True
        assert stats.uncertain == 2
        assert stats.confirmed == 0
        assert repo.get_entity(todo_a)["status"] == "open"
        assert repo.get_entity(todo_b)["status"] == "open"

    def test_out_of_range_pair_number_fails_open(
        self, memory_db: Database,
    ) -> None:
        engine = _make_engine(memory_db)
        repo = Repository(memory_db)
        todo_id = _insert_todo(repo, engine.project_id, "Implement the selector")
        driver_id = _insert_driver(
            repo, engine.project_id, "Implemented the selector",
        )
        candidate = _candidate(driver_id, todo_id)

        judge = ResolutionJudge(
            memory_db, _StubJudge(_verdict_response([(2, "CONFIRMED")])),
        )
        records, stats = judge.run([candidate])

        assert stats.judge_failed is True
        assert stats.uncertain == 1
        assert repo.get_entity(todo_id)["status"] == "open"

    def test_invalid_verdict_string_fails_open(
        self, memory_db: Database,
    ) -> None:
        engine = _make_engine(memory_db)
        repo = Repository(memory_db)
        todo_id = _insert_todo(repo, engine.project_id, "Implement the selector")
        driver_id = _insert_driver(
            repo, engine.project_id, "Implemented the selector",
        )
        candidate = _candidate(driver_id, todo_id)

        judge = ResolutionJudge(
            memory_db, _StubJudge(_verdict_response([(1, "MAYBE")])),
        )
        records, stats = judge.run([candidate])

        assert stats.judge_failed is True
        assert stats.uncertain == 1


class TestChunking:
    def test_one_llm_call_per_ten_candidates_ceiling(
        self, memory_db: Database,
    ) -> None:
        engine = _make_engine(memory_db)
        repo = Repository(memory_db)
        candidates = []
        for i in range(25):
            todo_id = _insert_todo(repo, engine.project_id, f"todo {i}")
            driver_id = _insert_driver(repo, engine.project_id, f"driver {i}")
            candidates.append(_candidate(driver_id, todo_id))

        # 3 chunks: 10 + 10 + 5. Every chunk answers UNCERTAIN so nothing
        # closes and no ordering assumptions are needed.
        responses = [
            _verdict_response([(i, "UNCERTAIN") for i in range(1, 11)]),
            _verdict_response([(i, "UNCERTAIN") for i in range(1, 11)]),
            _verdict_response([(i, "UNCERTAIN") for i in range(1, 6)]),
        ]
        judge = ResolutionJudge(memory_db, _SequencedJudge(responses))
        records, stats = judge.run(candidates)

        assert len(judge.llm.calls) == 3
        assert len(records) == 25
        assert stats.uncertain == 25


class TestDryRun:
    def test_dry_run_does_not_touch_db_but_reports_verdict(
        self, memory_db: Database,
    ) -> None:
        engine = _make_engine(memory_db)
        repo = Repository(memory_db)
        todo_id = _insert_todo(repo, engine.project_id, "Implement the selector")
        driver_id = _insert_driver(
            repo, engine.project_id, "Implemented the selector",
        )
        candidate = _candidate(driver_id, todo_id)

        judge = ResolutionJudge(
            memory_db, _StubJudge(_verdict_response([(1, "CONFIRMED")])),
        )
        records, stats = judge.run([candidate], dry_run=True)

        assert stats.confirmed == 1
        assert records[0]["verdict"] == "CONFIRMED"
        row = repo.get_entity(todo_id)
        assert row["status"] == "open"
