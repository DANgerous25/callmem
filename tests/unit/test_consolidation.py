"""Tests for LLM-routed consolidation (ADD/UPDATE/NOOP)."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING
from unittest.mock import patch

from callmem.core.consolidation import EntityConsolidator
from callmem.core.embeddings import Embedder, embedding_model_key, pack_vector
from callmem.core.engine import MemoryEngine
from callmem.core.extraction import EntityExtractor
from callmem.core.ollama import OllamaClient
from callmem.models.config import Config
from callmem.models.entities import Entity

if TYPE_CHECKING:
    from callmem.core.database import Database
    from callmem.core.repository import Repository


class _StubJudge:
    """Deterministic judge stand-in: records every prompt it is asked."""

    def __init__(self, response: str | None) -> None:
        self.response = response
        self.calls: list[str] = []

    def extract(self, prompt: str) -> str | None:
        self.calls.append(prompt)
        return self.response


def _make_engine(memory_db: Database) -> MemoryEngine:
    config = Config(sensitive_data={"enabled": False, "llm_scan": False})
    return MemoryEngine(memory_db, config)


def _insert_entity(
    repo: Repository,
    project_id: str,
    etype: str,
    title: str,
    content: str,
) -> str:
    entity = Entity(project_id=project_id, type=etype, title=title, content=content)
    row = entity.to_row()
    conn = repo.db.connect()
    try:
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
    return entity.id


def _judge_response(verdict: str, new_id: str, existing_id: str | None) -> str:
    return json.dumps([
        {
            "new_id": new_id, "verdict": verdict,
            "existing_id": existing_id, "reason": "stubbed",
        },
    ])


class TestSchema:
    def test_consolidation_log_table_exists(self, memory_db: Database) -> None:
        assert memory_db.get_schema_version() == 21
        conn = memory_db.connect()
        try:
            row = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='consolidation_log'",
            ).fetchone()
        finally:
            conn.close()
        assert row is not None


class TestConfigDefaults:
    def test_consolidation_config_defaults(self) -> None:
        config = Config()
        assert config.consolidation.enabled is True
        assert 0.0 < config.consolidation.threshold <= 1.0
        assert config.consolidation.top_k == 5


class TestAddVerdict:
    def test_no_similar_candidates_stays_add_without_llm_call(
        self, memory_db: Database,
    ) -> None:
        """Nothing above threshold -- entity stays ADD and the judge is
        never even called."""
        engine = _make_engine(memory_db)
        new_entity = Entity(
            project_id=engine.project_id, type="fact",
            title="Completely unrelated topic zzz",
            content="Nothing like anything else in the corpus",
        )
        _insert_new(engine.repo, new_entity)
        judge = _StubJudge(response=None)
        consolidator = EntityConsolidator(memory_db, judge, engine.config)

        stats = consolidator.consolidate(engine.project_id, [new_entity])

        assert judge.calls == []
        assert stats.added == 1
        assert stats.updated == 0
        assert stats.noop == 0
        assert stats.judge_failed is False

    def test_judge_add_verdict_keeps_both_entities_untouched(
        self, memory_db: Database,
    ) -> None:
        engine = _make_engine(memory_db)
        old_id = _insert_entity(
            engine.repo, engine.project_id, "fact",
            "Auth uses JWT tokens", "signed with RS256",
        )
        new_entity = Entity(
            project_id=engine.project_id, type="fact",
            title="Auth uses JWT tokens", content="signed with RS256",
        )
        _insert_new(engine.repo, new_entity)
        judge = _StubJudge(_judge_response("ADD", new_entity.id, None))
        consolidator = EntityConsolidator(memory_db, judge, engine.config)

        stats = consolidator.consolidate(engine.project_id, [new_entity])

        assert len(judge.calls) == 1
        assert stats.added == 1
        assert stats.updated == 0
        assert stats.noop == 0

        old_row = engine.repo.get_entity(old_id)
        assert old_row["stale"] == 0
        assert old_row["archived_at"] is None


class TestUpdateVerdict:
    def test_update_supersedes_old_and_keeps_new(
        self, memory_db: Database,
    ) -> None:
        engine = _make_engine(memory_db)
        old_id = _insert_entity(
            engine.repo, engine.project_id, "decision",
            "Use Redis for caching", "Chose Redis because it is fast",
        )
        new_entity = Entity(
            project_id=engine.project_id, type="decision",
            title="Use Redis for caching",
            content="Chose Redis with a 1-hour TTL after benchmarking",
        )
        _insert_new(engine.repo, new_entity)

        judge = _StubJudge(_judge_response("UPDATE", new_entity.id, old_id))
        consolidator = EntityConsolidator(memory_db, judge, engine.config)

        stats = consolidator.consolidate(engine.project_id, [new_entity])

        assert stats.updated == 1
        assert stats.added == 0
        assert stats.noop == 0

        old_row = engine.repo.get_entity(old_id)
        assert old_row["stale"] == 1
        assert old_row["superseded_by"] == new_entity.id
        assert old_row["staleness_reason"] == "consolidated"
        assert old_row["archived_at"] is None  # non-destructive: not deleted

        new_row = engine.repo.get_entity(new_entity.id)
        assert new_row["archived_at"] is None
        assert new_row["stale"] == 0


class TestNoopVerdict:
    def test_noop_archives_new_and_bumps_old_updated_at(
        self, memory_db: Database,
    ) -> None:
        engine = _make_engine(memory_db)
        old_id = _insert_entity(
            engine.repo, engine.project_id, "todo",
            "Add pagination tests", "Write integration tests",
        )
        old_before = engine.repo.get_entity(old_id)

        new_entity = Entity(
            project_id=engine.project_id, type="todo",
            title="Add pagination tests", content="Write integration tests",
        )
        _insert_new(engine.repo, new_entity)

        judge = _StubJudge(_judge_response("NOOP", new_entity.id, old_id))
        consolidator = EntityConsolidator(memory_db, judge, engine.config)

        stats = consolidator.consolidate(engine.project_id, [new_entity])

        assert stats.noop == 1
        assert stats.added == 0
        assert stats.updated == 0

        new_row = engine.repo.get_entity(new_entity.id)
        assert new_row["archived_at"] is not None  # archived, not deleted
        assert new_row["stale"] == 0  # non-destructive: no delete, no stale flag

        old_row = engine.repo.get_entity(old_id)
        # Bumped (SQLite's `datetime('now')` differs in format from the
        # Python-side ISO timestamp the row started with, so any change
        # in value already proves the bump happened).
        assert old_row["updated_at"] != old_before["updated_at"]
        assert old_row["archived_at"] is None
        assert old_row["stale"] == 0


class TestFailOpen:
    def test_malformed_judge_output_keeps_everything_as_add(
        self, memory_db: Database, caplog,
    ) -> None:
        engine = _make_engine(memory_db)
        old_id = _insert_entity(
            engine.repo, engine.project_id, "fact",
            "Queue backend is Redis", "used for background jobs",
        )
        new_entity = Entity(
            project_id=engine.project_id, type="fact",
            title="Queue backend is Redis", content="used for background jobs",
        )
        _insert_new(engine.repo, new_entity)

        judge = _StubJudge(response="not valid json at all")
        consolidator = EntityConsolidator(memory_db, judge, engine.config)

        with caplog.at_level(logging.WARNING):
            stats = consolidator.consolidate(engine.project_id, [new_entity])

        assert stats.added == 1
        assert stats.updated == 0
        assert stats.noop == 0
        assert stats.judge_failed is True
        assert any(
            "malformed" in r.message.lower() or "fail-open" in r.message.lower()
            for r in caplog.records
        )

        # Nothing destroyed or altered on either side.
        old_row = engine.repo.get_entity(old_id)
        assert old_row["stale"] == 0
        assert old_row["archived_at"] is None
        new_row = engine.repo.get_entity(new_entity.id)
        assert new_row["archived_at"] is None

    def test_absent_judge_response_keeps_everything_as_add(
        self, memory_db: Database,
    ) -> None:
        engine = _make_engine(memory_db)
        _insert_entity(
            engine.repo, engine.project_id, "fact",
            "Queue backend is Redis", "used for background jobs",
        )
        new_entity = Entity(
            project_id=engine.project_id, type="fact",
            title="Queue backend is Redis", content="used for background jobs",
        )
        _insert_new(engine.repo, new_entity)

        judge = _StubJudge(response=None)
        consolidator = EntityConsolidator(memory_db, judge, engine.config)
        stats = consolidator.consolidate(engine.project_id, [new_entity])

        assert stats.added == 1
        assert stats.judge_failed is True

    def test_partial_response_missing_an_entity_fails_open(
        self, memory_db: Database,
    ) -> None:
        """The judge answers for one qualifying entity but not the other --
        treated as malformed, not as a partial success."""
        engine = _make_engine(memory_db)
        old_a = _insert_entity(
            engine.repo, engine.project_id, "fact", "Auth uses JWT", "RS256",
        )
        old_b = _insert_entity(
            engine.repo, engine.project_id, "fact", "DB is SQLite", "WAL mode",
        )
        new_a = Entity(
            project_id=engine.project_id, type="fact",
            title="Auth uses JWT", content="RS256",
        )
        new_b = Entity(
            project_id=engine.project_id, type="fact",
            title="DB is SQLite", content="WAL mode",
        )
        _insert_new(engine.repo, new_a)
        _insert_new(engine.repo, new_b)

        # Only answers for new_a -- new_b is missing entirely.
        response = json.dumps([
            {"new_id": new_a.id, "verdict": "ADD", "existing_id": None},
        ])
        judge = _StubJudge(response=response)
        consolidator = EntityConsolidator(memory_db, judge, engine.config)
        stats = consolidator.consolidate(engine.project_id, [new_a, new_b])

        assert stats.judge_failed is True
        assert stats.added == 2
        assert engine.repo.get_entity(old_a)["stale"] == 0
        assert engine.repo.get_entity(old_b)["stale"] == 0


class TestThreshold:
    def test_below_threshold_candidate_never_reaches_the_judge(
        self, memory_db: Database,
    ) -> None:
        engine = _make_engine(memory_db)
        _insert_entity(
            engine.repo, engine.project_id, "fact",
            "Completely different unrelated topic about turtles",
            "nothing in common",
        )
        new_entity = Entity(
            project_id=engine.project_id, type="fact",
            title="Auth uses JWT tokens signed with RS256 keys",
            content="rotated quarterly",
        )
        config = engine.config.model_copy(deep=True)
        config.consolidation.threshold = 0.99  # unreachable by title similarity
        judge = _StubJudge(response=None)
        consolidator = EntityConsolidator(memory_db, judge, config)

        stats = consolidator.consolidate(engine.project_id, [new_entity])

        assert judge.calls == []
        assert stats.added == 1

    def test_low_threshold_routes_loosely_similar_titles_to_judge(
        self, memory_db: Database,
    ) -> None:
        engine = _make_engine(memory_db)
        _insert_entity(
            engine.repo, engine.project_id, "fact",
            "Auth uses JWT tokens", "RS256",
        )
        new_entity = Entity(
            project_id=engine.project_id, type="fact",
            title="Auth uses session cookies", content="cookie-backed",
        )
        config = engine.config.model_copy(deep=True)
        config.consolidation.threshold = 0.1  # very permissive
        judge = _StubJudge(_judge_response("ADD", new_entity.id, None))
        consolidator = EntityConsolidator(memory_db, judge, config)

        consolidator.consolidate(engine.project_id, [new_entity])

        assert len(judge.calls) == 1


class TestOneCallPerBatch:
    def test_multiple_qualifying_entities_share_one_llm_call(
        self, memory_db: Database,
    ) -> None:
        engine = _make_engine(memory_db)
        old_a = _insert_entity(
            engine.repo, engine.project_id, "fact", "Auth uses JWT", "RS256",
        )
        old_b = _insert_entity(
            engine.repo, engine.project_id, "fact", "DB is SQLite", "WAL mode",
        )
        new_a = Entity(
            project_id=engine.project_id, type="fact",
            title="Auth uses JWT", content="RS256",
        )
        new_b = Entity(
            project_id=engine.project_id, type="fact",
            title="DB is SQLite", content="WAL mode",
        )
        _insert_new(engine.repo, new_a)
        _insert_new(engine.repo, new_b)

        response = json.dumps([
            {"new_id": new_a.id, "verdict": "NOOP", "existing_id": old_a},
            {"new_id": new_b.id, "verdict": "UPDATE", "existing_id": old_b},
        ])
        judge = _StubJudge(response=response)
        consolidator = EntityConsolidator(memory_db, judge, engine.config)

        stats = consolidator.consolidate(engine.project_id, [new_a, new_b])

        assert len(judge.calls) == 1
        assert new_a.id in judge.calls[0]
        assert new_b.id in judge.calls[0]
        assert stats.noop == 1
        assert stats.updated == 1


class _StubEmbedder(Embedder):
    """Deterministic embedder stand-in -- returns the same fixed vector for
    every text, so tests control similarity by choosing that vector."""

    model = "stub-embed"

    def __init__(self, vector: list[float]) -> None:
        self._vector = vector

    def embed(
        self, texts: list[str], timeout: float | None = None,
    ) -> list[list[float]] | None:
        return [list(self._vector) for _ in texts]

    def is_available(self) -> bool:
        return True


class TestVectorSimilarity:
    """The vector-backed path (used when this project has stored
    embeddings) must find and correctly score candidates -- exercised
    separately from the FTS-fallback tests above, which never store
    embeddings and so never touch this code path."""

    def test_vector_path_finds_and_scores_similar_entity(
        self, memory_db: Database,
    ) -> None:
        engine = _make_engine(memory_db)
        old_id = _insert_entity(
            engine.repo, engine.project_id, "fact",
            "Auth uses JWT tokens", "signed with RS256",
        )
        model_key = embedding_model_key(engine.config)
        vector = [1.0, 0.0, 0.0]
        engine.repo.upsert_embedding(
            old_id, model_key, len(vector), pack_vector(vector),
        )

        # Title/content deliberately share no words with the old entity --
        # only the identical embedding vector should surface it as similar.
        new_entity = Entity(
            project_id=engine.project_id, type="fact",
            title="Totally different wording", content="Nothing alike here",
        )
        _insert_new(engine.repo, new_entity)

        judge = _StubJudge(_judge_response("UPDATE", new_entity.id, old_id))
        embedder = _StubEmbedder(vector)  # identical vector -> cosine == 1.0
        consolidator = EntityConsolidator(
            memory_db, judge, engine.config, embedder=embedder,
        )

        stats = consolidator.consolidate(engine.project_id, [new_entity])

        assert len(judge.calls) == 1
        assert old_id in judge.calls[0]
        assert stats.updated == 1
        old_row = engine.repo.get_entity(old_id)
        assert old_row["stale"] == 1
        assert old_row["superseded_by"] == new_entity.id

    def test_vector_path_respects_threshold(self, memory_db: Database) -> None:
        engine = _make_engine(memory_db)
        old_id = _insert_entity(
            engine.repo, engine.project_id, "fact",
            "Auth uses JWT tokens", "signed with RS256",
        )
        model_key = embedding_model_key(engine.config)
        engine.repo.upsert_embedding(
            old_id, model_key, 3, pack_vector([1.0, 0.0, 0.0]),
        )

        new_entity = Entity(
            project_id=engine.project_id, type="fact",
            title="Unrelated", content="Unrelated",
        )
        _insert_new(engine.repo, new_entity)

        # Orthogonal vector -> cosine == 0.0, far below any sane threshold.
        judge = _StubJudge(response=None)
        embedder = _StubEmbedder([0.0, 1.0, 0.0])
        consolidator = EntityConsolidator(
            memory_db, judge, engine.config, embedder=embedder,
        )

        stats = consolidator.consolidate(engine.project_id, [new_entity])

        assert judge.calls == []
        assert stats.added == 1


class TestDisabled:
    def test_disabled_config_skips_entirely(self, memory_db: Database) -> None:
        engine = _make_engine(memory_db)
        _insert_entity(
            engine.repo, engine.project_id, "fact", "Auth uses JWT", "RS256",
        )
        new_entity = Entity(
            project_id=engine.project_id, type="fact",
            title="Auth uses JWT", content="RS256",
        )
        config = engine.config.model_copy(deep=True)
        config.consolidation.enabled = False
        judge = _StubJudge(_judge_response("NOOP", new_entity.id, "whatever"))
        consolidator = EntityConsolidator(memory_db, judge, config)

        stats = consolidator.consolidate(engine.project_id, [new_entity])

        assert judge.calls == []
        assert stats == stats.__class__()


def _insert_new(repo: Repository, entity: Entity) -> None:
    """Persist a not-yet-consolidated "new" entity the same way the
    extractor does, so consolidation can look it up / archive it."""
    conn = repo.db.connect()
    row = entity.to_row()
    try:
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


class TestExtractionIntegration:
    """Consolidation runs at extraction-batch completion, scoped to the
    EntityExtractor path only (see task-2-report.md for why the other two
    entity-creation paths from Task 1 are out of scope for this task)."""

    def test_consolidate_called_after_extraction_with_config(
        self, memory_db: Database,
    ) -> None:
        config = Config(sensitive_data={"enabled": False, "llm_scan": False})
        engine = MemoryEngine(memory_db, config)
        extractor = EntityExtractor(memory_db, OllamaClient(), config=config)
        engine.start_session()
        engine.ingest_one("response", "Use SQLite for storage")

        llm_response = (
            '{"decisions": [{"title": "Use SQLite", "content": "Storage"}],'
            '"todos": [], "facts": [], "failures": [], "discoveries": [], '
            '"features": [], "bugfixes": [], "research": [], "changes": []}'
        )
        with patch.object(
            extractor.ollama, "_generate", return_value=llm_response,
        ), patch(
            "callmem.core.consolidation.EntityConsolidator.consolidate",
        ) as mock_consolidate:
            entities = extractor.process_pending()

        assert len(entities) == 1
        mock_consolidate.assert_called_once()
        call_args = mock_consolidate.call_args
        assert call_args[0][0] == engine.project_id
        assert [e.id for e in call_args[0][1]] == [entities[0].id]

    def test_no_config_skips_consolidation_without_error(
        self, memory_db: Database,
    ) -> None:
        """Matches the embeddings gate: without a config, the extractor
        cannot know whether consolidation is wanted, so it does nothing."""
        engine = MemoryEngine(
            memory_db, Config(sensitive_data={"enabled": False, "llm_scan": False}),
        )
        extractor = EntityExtractor(memory_db, OllamaClient())
        engine.start_session()
        engine.ingest_one("response", "Use SQLite for storage")

        llm_response = (
            '{"decisions": [{"title": "Use SQLite", "content": "Storage"}],'
            '"todos": [], "facts": [], "failures": [], "discoveries": [], '
            '"features": [], "bugfixes": [], "research": [], "changes": []}'
        )
        with patch.object(
            extractor.ollama, "_generate", return_value=llm_response,
        ):
            entities = extractor.process_pending()

        assert len(entities) == 1

    def test_consolidation_fault_does_not_fail_the_extraction_job(
        self, memory_db: Database,
    ) -> None:
        config = Config(sensitive_data={"enabled": False, "llm_scan": False})
        engine = MemoryEngine(memory_db, config)
        extractor = EntityExtractor(memory_db, OllamaClient(), config=config)
        engine.start_session()
        engine.ingest_one("response", "Use SQLite for storage")

        llm_response = (
            '{"decisions": [{"title": "Use SQLite", "content": "Storage"}],'
            '"todos": [], "facts": [], "failures": [], "discoveries": [], '
            '"features": [], "bugfixes": [], "research": [], "changes": []}'
        )
        with patch.object(
            extractor.ollama, "_generate", return_value=llm_response,
        ), patch(
            "callmem.core.consolidation.EntityConsolidator.consolidate",
            side_effect=RuntimeError("boom"),
        ):
            entities = extractor.process_pending()

        assert len(entities) == 1
