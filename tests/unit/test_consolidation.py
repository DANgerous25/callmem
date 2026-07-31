"""Tests for LLM-routed consolidation (ADD/UPDATE/NOOP)."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

from callmem.core.consolidation import ConsolidationStats, EntityConsolidator
from callmem.core.database import Database
from callmem.core.embeddings import Embedder, embedding_model_key, pack_vector
from callmem.core.engine import MemoryEngine
from callmem.core.extraction import EntityExtractor
from callmem.core.ollama import OllamaClient
from callmem.models.config import Config
from callmem.models.entities import Entity

if TYPE_CHECKING:
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
    """Engine with consolidation explicitly enabled -- the shipped default
    is off (see TestConfigDefaults / TestDisabledByDefault), so tests that
    exercise consolidation behavior opt in explicitly, same as a real
    project would in its config.toml."""
    config = Config(
        sensitive_data={"enabled": False, "llm_scan": False},
        consolidation={"enabled": True},
    )
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


def _judge_response(verdict: str, new_id: str, existing_id: str | None) -> str:
    return json.dumps([
        {
            "new_id": new_id, "verdict": verdict,
            "existing_id": existing_id, "reason": "stubbed",
        },
    ])


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


class _SpyEmbedder(Embedder):
    """Like _StubEmbedder, but records every call so tests can assert on
    how many separate embed() calls were made."""

    model = "stub-embed"

    def __init__(self, vector: list[float]) -> None:
        self._vector = vector
        self.calls: list[list[str]] = []

    def embed(
        self, texts: list[str], timeout: float | None = None,
    ) -> list[list[float]] | None:
        self.calls.append(list(texts))
        return [list(self._vector) for _ in texts]

    def is_available(self) -> bool:
        return True


class TestSchema:
    def test_consolidation_log_table_exists(self, memory_db: Database) -> None:
        assert memory_db.get_schema_version() == 22
        conn = memory_db.connect()
        try:
            row = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='consolidation_log'",
            ).fetchone()
        finally:
            conn.close()
        assert row is not None

    def test_entities_have_invalidated_at_column(self, memory_db: Database) -> None:
        conn = memory_db.connect()
        try:
            cols = [r["name"] for r in conn.execute("PRAGMA table_info(entities)")]
        finally:
            conn.close()
        assert "invalidated_at" in cols

    def test_consolidation_log_has_contradicted_column(
        self, memory_db: Database,
    ) -> None:
        conn = memory_db.connect()
        try:
            cols = [
                r["name"] for r in conn.execute(
                    "PRAGMA table_info(consolidation_log)",
                )
            ]
        finally:
            conn.close()
        assert "contradicted" in cols


class TestConfigDefaults:
    def test_consolidation_config_defaults(self) -> None:
        config = Config()
        # Off by default: the threshold is uncalibrated and this feature
        # archives/marks-stale entities in a project's existing memory.
        assert config.consolidation.enabled is False
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


class TestContradictsVerdict:
    def test_contradicts_marks_existing_stale_with_invalidated_at(
        self, memory_db: Database,
    ) -> None:
        engine = _make_engine(memory_db)
        old_id = _insert_entity(
            engine.repo, engine.project_id, "fact",
            "DB is SQLite", "single-writer, WAL mode",
        )
        new_entity = Entity(
            project_id=engine.project_id, type="fact",
            title="DB is SQLite",
            content="Actually Postgres now -- SQLite dropped for concurrency",
        )
        _insert_new(engine.repo, new_entity)

        judge = _StubJudge(_judge_response("CONTRADICTS", new_entity.id, old_id))
        consolidator = EntityConsolidator(memory_db, judge, engine.config)

        stats = consolidator.consolidate(engine.project_id, [new_entity])

        assert stats.contradicted == 1
        assert stats.added == 0
        assert stats.updated == 0
        assert stats.noop == 0

        old_row = engine.repo.get_entity(old_id)
        assert old_row["stale"] == 1
        assert old_row["staleness_reason"] == "contradicted"
        assert old_row["superseded_by"] == new_entity.id
        assert old_row["invalidated_at"] is not None
        assert old_row["archived_at"] is None  # non-destructive

        # Both entries are kept -- unlike UPDATE, the new entity isn't
        # a refinement, it's an independent, conflicting fact.
        new_row = engine.repo.get_entity(new_entity.id)
        assert new_row["archived_at"] is None
        assert new_row["stale"] == 0

    def test_contradicts_id_validation_matches_update(
        self, memory_db: Database,
    ) -> None:
        """Same guard as UPDATE: existing_id must be a string drawn from
        that entity's own candidate list, not invented or borrowed from
        a batch sibling."""
        engine = _make_engine(memory_db)
        _insert_entity(
            engine.repo, engine.project_id, "fact", "Auth uses JWT", "RS256",
        )
        new_entity = Entity(
            project_id=engine.project_id, type="fact",
            title="Auth uses JWT", content="RS256",
        )
        _insert_new(engine.repo, new_entity)

        response = json.dumps([{
            "new_id": new_entity.id, "verdict": "CONTRADICTS",
            "existing_id": "not-a-real-candidate-id",
        }])
        judge = _StubJudge(response=response)
        consolidator = EntityConsolidator(memory_db, judge, engine.config)
        stats = consolidator.consolidate(engine.project_id, [new_entity])

        assert stats.judge_failed is True
        assert stats.added == 1
        assert stats.contradicted == 0

    def test_contradicts_rejects_existing_id_naming_a_batch_sibling(
        self, memory_db: Database,
    ) -> None:
        engine = _make_engine(memory_db)
        _insert_entity(
            engine.repo, engine.project_id, "fact", "Auth uses JWT", "RS256",
        )
        entity_a = Entity(
            project_id=engine.project_id, type="fact",
            title="Auth uses JWT", content="RS256",
        )
        entity_b = Entity(
            project_id=engine.project_id, type="fact",
            title="Auth uses JWT", content="RS256",
        )
        _insert_new(engine.repo, entity_a)
        _insert_new(engine.repo, entity_b)

        response = json.dumps([
            {
                "new_id": entity_a.id, "verdict": "CONTRADICTS",
                "existing_id": entity_b.id,
            },
        ])
        judge = _StubJudge(response=response)
        consolidator = EntityConsolidator(memory_db, judge, engine.config)
        stats = consolidator.consolidate(
            engine.project_id, [entity_a, entity_b],
        )

        assert stats.judge_failed is True
        assert stats.added == 2
        assert stats.contradicted == 0

    def test_contradicts_on_already_stale_target_is_checked_noop(
        self, memory_db: Database, caplog,
    ) -> None:
        """Two new entities both CONTRADICT the same existing entity.
        Only the first invalidation actually happens (mark_stale's
        WHERE stale=0 guard); the second must not be double-counted, and
        the collision must be logged (the CONTRADICTS branch uses the
        same _log_duplicate_claim as UPDATE)."""
        engine = _make_engine(memory_db)
        old_id = _insert_entity(
            engine.repo, engine.project_id, "fact", "Region is us-east", "x",
        )
        entity_a = Entity(
            project_id=engine.project_id, type="fact",
            title="Region is us-east", content="Moved to eu-west",
        )
        entity_b = Entity(
            project_id=engine.project_id, type="fact",
            title="Region is us-east", content="Moved to ap-south",
        )
        _insert_new(engine.repo, entity_a)
        _insert_new(engine.repo, entity_b)

        response = json.dumps([
            {
                "new_id": entity_a.id, "verdict": "CONTRADICTS",
                "existing_id": old_id,
            },
            {
                "new_id": entity_b.id, "verdict": "CONTRADICTS",
                "existing_id": old_id,
            },
        ])
        judge = _StubJudge(response=response)
        consolidator = EntityConsolidator(memory_db, judge, engine.config)

        with caplog.at_level(logging.WARNING):
            stats = consolidator.consolidate(
                engine.project_id, [entity_a, entity_b],
            )

        # Only one actual invalidation happened -- the second CONTRADICTS
        # found the target already stale and is a checked no-op.
        assert stats.contradicted == 1
        assert stats.added == 1
        collision_logs = [
            r.message for r in caplog.records
            if entity_b.id in r.message and old_id in r.message
        ]
        assert collision_logs, caplog.records

        old_row = engine.repo.get_entity(old_id)
        assert old_row["stale"] == 1
        # First writer wins.
        assert old_row["superseded_by"] == entity_a.id

        # Neither new entity is archived or otherwise destroyed.
        assert engine.repo.get_entity(entity_a.id)["archived_at"] is None
        assert engine.repo.get_entity(entity_b.id)["archived_at"] is None

    def test_consolidation_log_records_contradicted_count(
        self, memory_db: Database,
    ) -> None:
        engine = _make_engine(memory_db)
        old_id = _insert_entity(
            engine.repo, engine.project_id, "fact", "Auth uses JWT", "RS256",
        )
        new_entity = Entity(
            project_id=engine.project_id, type="fact",
            title="Auth uses JWT", content="Actually mTLS now",
        )
        _insert_new(engine.repo, new_entity)

        judge = _StubJudge(_judge_response("CONTRADICTS", new_entity.id, old_id))
        consolidator = EntityConsolidator(memory_db, judge, engine.config)
        consolidator.consolidate(engine.project_id, [new_entity])

        conn = memory_db.connect()
        try:
            row = conn.execute(
                "SELECT contradicted FROM consolidation_log "
                "WHERE project_id = ? ORDER BY run_at DESC LIMIT 1",
                (engine.project_id,),
            ).fetchone()
        finally:
            conn.close()
        assert row["contradicted"] == 1


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

    def test_noop_touches_the_still_active_successor_not_a_stale_target(
        self, memory_db: Database,
    ) -> None:
        """Two different new entities in the same batch can legitimately
        share the same pre-existing candidate. If one UPDATEs it (marking
        it stale + superseded) before the other's NOOP is applied, the
        NOOP's touch must redirect to the still-active successor -- the
        survivor of a NOOP can never be an entity this same run just
        marked stale."""
        engine = _make_engine(memory_db)
        old_x = _insert_entity(
            engine.repo, engine.project_id, "fact",
            "Auth uses JWT tokens", "signed with RS256",
        )
        # Refines X.
        entity_c = Entity(
            project_id=engine.project_id, type="fact",
            title="Auth uses JWT tokens",
            content="signed with RS256, rotated quarterly",
        )
        # Duplicates X (judge also matches it to X, not to entity_c --
        # batch siblings are never candidates for each other).
        entity_d = Entity(
            project_id=engine.project_id, type="fact",
            title="Auth uses JWT tokens", content="signed with RS256",
        )
        _insert_new(engine.repo, entity_c)
        _insert_new(engine.repo, entity_d)
        entity_c_before = engine.repo.get_entity(entity_c.id)

        response = json.dumps([
            {"new_id": entity_c.id, "verdict": "UPDATE", "existing_id": old_x},
            {"new_id": entity_d.id, "verdict": "NOOP", "existing_id": old_x},
        ])
        judge = _StubJudge(response=response)
        consolidator = EntityConsolidator(memory_db, judge, engine.config)

        stats = consolidator.consolidate(
            engine.project_id, [entity_c, entity_d],
        )

        assert stats.updated == 1
        assert stats.noop == 1

        old_x_row = engine.repo.get_entity(old_x)
        assert old_x_row["stale"] == 1
        assert old_x_row["superseded_by"] == entity_c.id

        entity_d_row = engine.repo.get_entity(entity_d.id)
        assert entity_d_row["archived_at"] is not None

        # Redirected: entity_c (the active successor), not the now-stale
        # old_x, is the one that got bumped by the NOOP.
        entity_c_row = engine.repo.get_entity(entity_c.id)
        assert entity_c_row["updated_at"] != entity_c_before["updated_at"]


class TestCitationTransfer:
    """Task 3: consolidation must not strand cited_count on an entity it
    archives or supersedes -- briefing importance ranking treats
    cited_count as a real signal, so losing it would make consolidation
    demote the very memory it chose to keep."""

    def test_noop_transfers_archived_duplicates_citations_to_survivor(
        self, memory_db: Database,
    ) -> None:
        engine = _make_engine(memory_db)
        old_id = _insert_entity(
            engine.repo, engine.project_id, "todo",
            "Add pagination tests", "Write integration tests",
        )
        engine.repo.set_citation_counts({old_id: (2, "2026-01-01T00:00:00")})

        new_entity = Entity(
            project_id=engine.project_id, type="todo",
            title="Add pagination tests", content="Write integration tests",
        )
        _insert_new(engine.repo, new_entity)
        engine.repo.set_citation_counts({
            new_entity.id: (5, "2026-01-10T00:00:00"),
        })

        judge = _StubJudge(_judge_response("NOOP", new_entity.id, old_id))
        consolidator = EntityConsolidator(memory_db, judge, engine.config)
        stats = consolidator.consolidate(engine.project_id, [new_entity])

        assert stats.noop == 1
        assert stats.transfer_attempts == 1
        survivor_row = engine.repo.get_entity(old_id)
        assert survivor_row["cited_count"] == 7
        assert survivor_row["last_cited_at"] == "2026-01-10T00:00:00"

        # The archived duplicate's own count is zeroed, not left to be
        # double-counted if this entity were ever processed again.
        archived_row = engine.repo.get_entity(new_entity.id)
        assert archived_row["cited_count"] == 0

    def test_noop_archive_links_superseded_by_so_post_hoc_citations_are_not_stranded(
        self, memory_db: Database,
    ) -> None:
        """Completes the citation-stranding fix: before this, archive_entity
        never set superseded_by, so a NOOP-archived duplicate had no link
        for `_resolve_live_citation_target` to follow -- a citation
        arriving AFTER the archival (the post-hoc case that fix targets)
        stayed stranded on the dead row forever instead of crediting the
        survivor."""
        engine = _make_engine(memory_db)
        old_id = _insert_entity(
            engine.repo, engine.project_id, "todo", "dup title", "content",
        )
        new_entity = Entity(
            project_id=engine.project_id, type="todo",
            title="dup title", content="content",
        )
        _insert_new(engine.repo, new_entity)

        judge = _StubJudge(_judge_response("NOOP", new_entity.id, old_id))
        consolidator = EntityConsolidator(memory_db, judge, engine.config)
        stats = consolidator.consolidate(engine.project_id, [new_entity])
        assert stats.noop == 1

        archived_row = engine.repo.get_entity(new_entity.id)
        assert archived_row["superseded_by"] == old_id

        # A citation lands on the archived duplicate after the fact.
        engine.repo.increment_citation_counts(
            {new_entity.id: (4, "2026-01-15T00:00:00")},
        )

        assert engine.repo.get_entity(old_id)["cited_count"] == 4
        assert engine.repo.get_entity(new_entity.id)["cited_count"] == 0

    def test_noop_zero_citation_duplicate_does_not_change_survivors_count(
        self, memory_db: Database,
    ) -> None:
        engine = _make_engine(memory_db)
        old_id = _insert_entity(
            engine.repo, engine.project_id, "todo", "dup title", "content",
        )
        engine.repo.set_citation_counts({old_id: (3, "2026-01-01T00:00:00")})

        new_entity = Entity(
            project_id=engine.project_id, type="todo",
            title="dup title", content="content",
        )
        _insert_new(engine.repo, new_entity)  # never cited: cited_count == 0

        judge = _StubJudge(_judge_response("NOOP", new_entity.id, old_id))
        consolidator = EntityConsolidator(memory_db, judge, engine.config)
        consolidator.consolidate(engine.project_id, [new_entity])

        survivor_row = engine.repo.get_entity(old_id)
        assert survivor_row["cited_count"] == 3
        assert survivor_row["last_cited_at"] == "2026-01-01T00:00:00"

    def test_update_transfers_old_entitys_citations_to_new_survivor(
        self, memory_db: Database,
    ) -> None:
        engine = _make_engine(memory_db)
        old_id = _insert_entity(
            engine.repo, engine.project_id, "decision",
            "Use Redis for caching", "Chose Redis because it is fast",
        )
        engine.repo.set_citation_counts({old_id: (4, "2026-02-01T00:00:00")})

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
        assert stats.transfer_attempts == 1
        new_row = engine.repo.get_entity(new_entity.id)
        assert new_row["cited_count"] == 4
        assert new_row["last_cited_at"] == "2026-02-01T00:00:00"

        # The superseded entity's own count is zeroed.
        old_row = engine.repo.get_entity(old_id)
        assert old_row["cited_count"] == 0

    def test_contradicts_transfers_old_entitys_citations_to_new_survivor(
        self, memory_db: Database,
    ) -> None:
        """CONTRADICTS retires the old entity via `stale` the same as
        UPDATE -- it's excluded from briefings either way, so its
        citation credit belongs to the entity that replaces it rather
        than staying stranded on a fact `callmem stale` now lists as
        contradicted."""
        engine = _make_engine(memory_db)
        old_id = _insert_entity(
            engine.repo, engine.project_id, "fact", "DB is SQLite", "x",
        )
        engine.repo.set_citation_counts({old_id: (6, "2026-03-01T00:00:00")})

        new_entity = Entity(
            project_id=engine.project_id, type="fact",
            title="DB is SQLite", content="Actually Postgres now",
        )
        _insert_new(engine.repo, new_entity)

        judge = _StubJudge(_judge_response("CONTRADICTS", new_entity.id, old_id))
        consolidator = EntityConsolidator(memory_db, judge, engine.config)
        stats = consolidator.consolidate(engine.project_id, [new_entity])

        assert stats.contradicted == 1
        assert stats.transfer_attempts == 1
        new_row = engine.repo.get_entity(new_entity.id)
        assert new_row["cited_count"] == 6
        assert new_row["last_cited_at"] == "2026-03-01T00:00:00"

        old_row = engine.repo.get_entity(old_id)
        assert old_row["cited_count"] == 0
        assert old_row["stale"] == 1
        assert old_row["invalidated_at"] is not None

    def test_entity_with_zero_citations_is_a_noop_transfer(
        self, memory_db: Database,
    ) -> None:
        """Neither side has ever been cited -- transfer must not error
        and must leave both sides at zero."""
        engine = _make_engine(memory_db)
        old_id = _insert_entity(
            engine.repo, engine.project_id, "decision", "Use Postgres", "x",
        )
        new_entity = Entity(
            project_id=engine.project_id, type="decision",
            title="Use Postgres", content="x, with pgbouncer",
        )
        _insert_new(engine.repo, new_entity)

        judge = _StubJudge(_judge_response("UPDATE", new_entity.id, old_id))
        consolidator = EntityConsolidator(memory_db, judge, engine.config)
        consolidator.consolidate(engine.project_id, [new_entity])

        assert engine.repo.get_entity(new_entity.id)["cited_count"] == 0
        assert engine.repo.get_entity(old_id)["cited_count"] == 0

    def test_reprocessing_the_same_decisions_does_not_double_count(
        self, memory_db: Database,
    ) -> None:
        """Guards the idempotency requirement directly: if _apply were
        ever somehow invoked twice over the same batch/decisions (a bug
        elsewhere, a retried job, ...), the citation transfer must not
        double-count -- transfer_citations zeroes the source as part of
        its own statement, so a repeat transfer adds zero."""
        engine = _make_engine(memory_db)
        old_id = _insert_entity(
            engine.repo, engine.project_id, "todo", "dup title", "content",
        )
        engine.repo.set_citation_counts({old_id: (2, "2026-01-01T00:00:00")})

        new_entity = Entity(
            project_id=engine.project_id, type="todo",
            title="dup title", content="content",
        )
        _insert_new(engine.repo, new_entity)
        engine.repo.set_citation_counts({
            new_entity.id: (5, "2026-01-10T00:00:00"),
        })

        judge = _StubJudge(_judge_response("NOOP", new_entity.id, old_id))
        consolidator = EntityConsolidator(memory_db, judge, engine.config)
        decisions = {new_entity.id: ("NOOP", old_id, "stubbed")}

        first = consolidator._apply([new_entity], decisions)
        second = consolidator._apply([new_entity], decisions)  # simulated re-run

        survivor_row = engine.repo.get_entity(old_id)
        assert survivor_row["cited_count"] == 7

        # The gating layer: archive_entity's guard means the second pass
        # sees entity.id already archived and never re-runs the transfer
        # at all (belt to transfer_citations' own self-zeroing braces).
        assert first.noop == 1
        assert first.transfer_attempts == 1
        assert second.noop == 0
        assert second.transfer_attempts == 0
        assert second.added == 1


class TestApplyOrderIndependence:
    """_apply must resolve every UPDATE/CONTRADICTS supersession before any
    NOOP redirect is resolved, regardless of the order entities happen to
    appear in the batch -- and duplicate `existing_id` claims across two
    UPDATE (or CONTRADICTS) decisions must not overcount or desync stats
    from what the DB actually recorded."""

    def _run_update_noop_pair(self, entities_order: str) -> dict[str, Any]:
        """Same UPDATE+NOOP pair on the same target, run on a fresh DB in
        either entities order. Returns a role-keyed, id-normalized summary
        of the outcome so the two orders can be compared for equality
        without relying on cross-run timestamp equality (SQLite's
        ``datetime('now')`` has only second resolution, so comparing two
        independent runs' raw timestamps would be flaky; instead each run
        compares its own before/after)."""
        db = Database(":memory:")
        db.initialize()
        engine = _make_engine(db)
        old_x = _insert_entity(
            engine.repo, engine.project_id, "fact",
            "Auth uses JWT tokens", "signed with RS256",
        )
        entity_c = Entity(
            project_id=engine.project_id, type="fact",
            title="Auth uses JWT tokens",
            content="signed with RS256, rotated quarterly",
        )
        entity_d = Entity(
            project_id=engine.project_id, type="fact",
            title="Auth uses JWT tokens", content="signed with RS256",
        )
        _insert_new(engine.repo, entity_c)
        _insert_new(engine.repo, entity_d)
        entity_c_before = engine.repo.get_entity(entity_c.id)

        response = json.dumps([
            {"new_id": entity_c.id, "verdict": "UPDATE", "existing_id": old_x},
            {"new_id": entity_d.id, "verdict": "NOOP", "existing_id": old_x},
        ])
        judge = _StubJudge(response=response)
        consolidator = EntityConsolidator(db, judge, engine.config)

        ordered = (
            [entity_c, entity_d] if entities_order == "update_first"
            else [entity_d, entity_c]
        )
        stats = consolidator.consolidate(engine.project_id, ordered)

        old_x_row = engine.repo.get_entity(old_x)
        entity_c_row = engine.repo.get_entity(entity_c.id)
        entity_d_row = engine.repo.get_entity(entity_d.id)
        return {
            # The whole dataclass, not just a few hand-picked fields --
            # any stat the two orders disagree on should fail this
            # comparison, not just the ones someone remembered to list.
            "stats": stats,
            "old_x_stale": old_x_row["stale"],
            "old_x_superseded_by_is_entity_c": (
                old_x_row["superseded_by"] == entity_c.id
            ),
            "entity_d_archived": entity_d_row["archived_at"] is not None,
            "entity_c_was_touched": (
                entity_c_row["updated_at"] != entity_c_before["updated_at"]
            ),
        }

    def test_update_then_noop_and_noop_then_update_produce_identical_state(
        self,
    ) -> None:
        fwd = self._run_update_noop_pair("update_first")
        rev = self._run_update_noop_pair("noop_first")

        expected = {
            # transfer_attempts=2: the UPDATE transfer (old_x -> entity_c) and
            # the NOOP transfer (entity_d -> entity_c).
            "stats": ConsolidationStats(
                added=0, updated=1, noop=1, contradicted=0,
                judge_failed=False, transfer_attempts=2,
            ),
            "old_x_stale": 1,
            "old_x_superseded_by_is_entity_c": True,
            "entity_d_archived": True,
            # This is the assertion that fails pre-fix: in the
            # "noop_first" order the old code redirected the touch to
            # old_x itself (already doomed to go stale a moment later)
            # instead of entity_c, the live successor.
            "entity_c_was_touched": True,
        }
        assert fwd == expected
        assert rev == expected

    def _run_contradicts_noop_pair(self, entities_order: str) -> dict[str, Any]:
        """Same shape as ``_run_update_noop_pair`` but with CONTRADICTS in
        entity_c's role instead of UPDATE -- the same order-dependence bug
        (2a) applies to any verdict that can populate the survivor map,
        not just UPDATE, and it was previously untested for CONTRADICTS."""
        db = Database(":memory:")
        db.initialize()
        engine = _make_engine(db)
        old_x = _insert_entity(
            engine.repo, engine.project_id, "fact",
            "Region is us-east", "primary region",
        )
        entity_c = Entity(
            project_id=engine.project_id, type="fact",
            title="Region is us-east", content="Moved to eu-west",
        )
        entity_d = Entity(
            project_id=engine.project_id, type="fact",
            title="Region is us-east", content="primary region",
        )
        _insert_new(engine.repo, entity_c)
        _insert_new(engine.repo, entity_d)
        entity_c_before = engine.repo.get_entity(entity_c.id)

        response = json.dumps([
            {"new_id": entity_c.id, "verdict": "CONTRADICTS", "existing_id": old_x},
            {"new_id": entity_d.id, "verdict": "NOOP", "existing_id": old_x},
        ])
        judge = _StubJudge(response=response)
        consolidator = EntityConsolidator(db, judge, engine.config)

        ordered = (
            [entity_c, entity_d] if entities_order == "contradicts_first"
            else [entity_d, entity_c]
        )
        stats = consolidator.consolidate(engine.project_id, ordered)

        old_x_row = engine.repo.get_entity(old_x)
        entity_c_row = engine.repo.get_entity(entity_c.id)
        entity_d_row = engine.repo.get_entity(entity_d.id)
        return {
            "stats": stats,
            "old_x_stale": old_x_row["stale"],
            "old_x_invalidated": old_x_row["invalidated_at"] is not None,
            "old_x_superseded_by_is_entity_c": (
                old_x_row["superseded_by"] == entity_c.id
            ),
            "entity_d_archived": entity_d_row["archived_at"] is not None,
            "entity_c_was_touched": (
                entity_c_row["updated_at"] != entity_c_before["updated_at"]
            ),
        }

    def test_contradicts_then_noop_and_noop_then_contradicts_produce_identical_state(
        self,
    ) -> None:
        fwd = self._run_contradicts_noop_pair("contradicts_first")
        rev = self._run_contradicts_noop_pair("noop_first")

        expected = {
            # transfer_attempts=2: the CONTRADICTS transfer (old_x -> entity_c)
            # and the NOOP transfer (entity_d -> entity_c).
            "stats": ConsolidationStats(
                added=0, updated=0, noop=1, contradicted=1,
                judge_failed=False, transfer_attempts=2,
            ),
            "old_x_stale": 1,
            "old_x_invalidated": True,
            "old_x_superseded_by_is_entity_c": True,
            "entity_d_archived": True,
            "entity_c_was_touched": True,
        }
        assert fwd == expected
        assert rev == expected

    def test_noop_first_order_still_redirects_to_the_live_survivor(
        self, memory_db: Database,
    ) -> None:
        """Reverse of test_noop_touches_the_still_active_successor_not_a_
        stale_target: the NOOP appears BEFORE the UPDATE in `entities`.
        Pre-fix, this order silently lost the redirect (it touched old_x,
        which the very next step marked stale) -- the survivor of a NOOP
        must never end up being an entity this same run marked stale,
        no matter which order the batch lists them in."""
        engine = _make_engine(memory_db)
        old_x = _insert_entity(
            engine.repo, engine.project_id, "fact",
            "Auth uses JWT tokens", "signed with RS256",
        )
        entity_c = Entity(
            project_id=engine.project_id, type="fact",
            title="Auth uses JWT tokens",
            content="signed with RS256, rotated quarterly",
        )
        entity_d = Entity(
            project_id=engine.project_id, type="fact",
            title="Auth uses JWT tokens", content="signed with RS256",
        )
        _insert_new(engine.repo, entity_c)
        _insert_new(engine.repo, entity_d)
        entity_c_before = engine.repo.get_entity(entity_c.id)

        response = json.dumps([
            {"new_id": entity_c.id, "verdict": "UPDATE", "existing_id": old_x},
            {"new_id": entity_d.id, "verdict": "NOOP", "existing_id": old_x},
        ])
        judge = _StubJudge(response=response)
        consolidator = EntityConsolidator(memory_db, judge, engine.config)

        # NOOP entity listed FIRST -- the reverse of the passing test above.
        stats = consolidator.consolidate(
            engine.project_id, [entity_d, entity_c],
        )

        assert stats.updated == 1
        assert stats.noop == 1

        old_x_row = engine.repo.get_entity(old_x)
        assert old_x_row["stale"] == 1
        assert old_x_row["superseded_by"] == entity_c.id

        entity_d_row = engine.repo.get_entity(entity_d.id)
        assert entity_d_row["archived_at"] is not None

        # The redirect must land on entity_c (the live successor), not on
        # old_x (which this same run marked stale) -- entity_c's
        # updated_at must have moved from its pre-run value.
        entity_c_row = engine.repo.get_entity(entity_c.id)
        assert entity_c_row["updated_at"] != entity_c_before["updated_at"]

    def test_duplicate_existing_id_across_two_updates_counts_one_supersession(
        self, memory_db: Database, caplog,
    ) -> None:
        """Two different new entities both UPDATE the SAME existing_id.
        Only the first supersession actually happens in the DB (mark_stale's
        WHERE stale=0 guard); stats.updated must reflect that -- not double
        count -- and a later NOOP on the same target must redirect to
        whichever entity the DB actually recorded as the supersessor, not
        whichever entity happened to be processed last. The collision must
        also be logged -- an incoherent judge response naming two new
        entities as superseding the same memory is exactly the signal
        Task 4's calibration harness needs, and it must not vanish
        silently into ``stats.added``."""
        engine = _make_engine(memory_db)
        old_id = _insert_entity(
            engine.repo, engine.project_id, "fact",
            "Region is us-east", "primary region",
        )
        entity_a = Entity(
            project_id=engine.project_id, type="fact",
            title="Region is us-east", content="Moved to eu-west",
        )
        entity_b = Entity(
            project_id=engine.project_id, type="fact",
            title="Region is us-east", content="Moved to ap-south",
        )
        entity_e = Entity(
            project_id=engine.project_id, type="fact",
            title="Region is us-east", content="primary region",
        )
        _insert_new(engine.repo, entity_a)
        _insert_new(engine.repo, entity_b)
        _insert_new(engine.repo, entity_e)
        entity_a_before = engine.repo.get_entity(entity_a.id)
        entity_b_before = engine.repo.get_entity(entity_b.id)

        response = json.dumps([
            {"new_id": entity_a.id, "verdict": "UPDATE", "existing_id": old_id},
            {"new_id": entity_b.id, "verdict": "UPDATE", "existing_id": old_id},
            {"new_id": entity_e.id, "verdict": "NOOP", "existing_id": old_id},
        ])
        judge = _StubJudge(response=response)
        consolidator = EntityConsolidator(memory_db, judge, engine.config)

        with caplog.at_level(logging.WARNING):
            stats = consolidator.consolidate(
                engine.project_id, [entity_a, entity_b, entity_e],
            )

        # Exactly one supersession happened; the second UPDATE's target
        # was already stale, so it falls back to ADD rather than being
        # double-counted as updated.
        assert stats.updated == 1
        assert stats.added == 1
        assert stats.noop == 1

        # The collision is logged, naming the losing entity, the target
        # it contested, and the winner (entity_a) that beat it there.
        collision_logs = [
            r.message for r in caplog.records
            if entity_b.id in r.message
            and old_id in r.message
            and entity_a.id in r.message
        ]
        assert collision_logs, caplog.records

        old_row = engine.repo.get_entity(old_id)
        assert old_row["stale"] == 1
        # First writer wins -- the DB's superseded_by is entity_a, never
        # entity_b (which the pre-fix code would have overwritten it with
        # even though its mark_stale call was a no-op).
        assert old_row["superseded_by"] == entity_a.id

        # Neither UPDATE candidate is archived (non-destructive).
        assert engine.repo.get_entity(entity_a.id)["archived_at"] is None
        assert engine.repo.get_entity(entity_b.id)["archived_at"] is None

        # The NOOP archived entity_e and redirected the touch to the
        # entity the DB actually recorded as old_id's supersessor
        # (entity_a) -- NOT entity_b, which never actually superseded
        # anything.
        assert engine.repo.get_entity(entity_e.id)["archived_at"] is not None
        entity_a_row = engine.repo.get_entity(entity_a.id)
        entity_b_row = engine.repo.get_entity(entity_b.id)
        assert entity_a_row["updated_at"] != entity_a_before["updated_at"]
        assert entity_b_row["updated_at"] == entity_b_before["updated_at"]


class TestNoopLiveSurvivorGuard:
    """A NOOP's survivor must be re-verified live immediately before its
    duplicate is archived. Candidate lookup and the judge call both
    happen before ``_apply`` runs, so a concurrent process outside this
    batch (staleness detection, a manual resolve, another consolidation
    run) could archive or stale the survivor in between -- archiving the
    duplicate against a dead survivor would remove the fact from the
    visible corpus entirely, with no live copy left."""

    def test_archived_survivor_skips_the_archive_and_counts_as_add(
        self, memory_db: Database, caplog,
    ) -> None:
        engine = _make_engine(memory_db)
        old_id = _insert_entity(
            engine.repo, engine.project_id, "fact", "dup title", "content",
        )
        new_entity = Entity(
            project_id=engine.project_id, type="fact",
            title="dup title", content="content",
        )
        _insert_new(engine.repo, new_entity)

        # Simulate a concurrent process archiving the survivor between
        # the judge call and _apply -- nothing in this batch does this.
        engine.repo.archive_entity(old_id)

        consolidator = EntityConsolidator(
            memory_db, _StubJudge(response=None), engine.config,
        )
        decisions = {new_entity.id: ("NOOP", old_id, "stubbed")}

        with caplog.at_level(logging.WARNING):
            stats = consolidator._apply([new_entity], decisions)

        assert stats.noop == 0
        assert stats.added == 1
        assert any(
            "no longer live" in r.message for r in caplog.records
        )

        # Both entities left untouched -- the duplicate was NOT archived.
        assert engine.repo.get_entity(new_entity.id)["archived_at"] is None

    def test_staled_not_archived_survivor_also_skips_the_archive(
        self, memory_db: Database,
    ) -> None:
        """The liveness check is archived OR stale, not just archived."""
        engine = _make_engine(memory_db)
        old_id = _insert_entity(
            engine.repo, engine.project_id, "fact", "dup title", "content",
        )
        new_entity = Entity(
            project_id=engine.project_id, type="fact",
            title="dup title", content="content",
        )
        _insert_new(engine.repo, new_entity)

        # A manual stale mark from outside this batch -- not a
        # consolidation supersession, just any other staleness path.
        engine.repo.mark_stale(old_id, reason="manual")

        consolidator = EntityConsolidator(
            memory_db, _StubJudge(response=None), engine.config,
        )
        decisions = {new_entity.id: ("NOOP", old_id, "stubbed")}

        stats = consolidator._apply([new_entity], decisions)

        assert stats.noop == 0
        assert stats.added == 1
        assert engine.repo.get_entity(new_entity.id)["archived_at"] is None

    def test_live_survivor_still_archives_normally(
        self, memory_db: Database,
    ) -> None:
        """Control: an untouched, live survivor still gets the normal
        NOOP treatment -- the guard must not block the ordinary path."""
        engine = _make_engine(memory_db)
        old_id = _insert_entity(
            engine.repo, engine.project_id, "fact", "dup title", "content",
        )
        new_entity = Entity(
            project_id=engine.project_id, type="fact",
            title="dup title", content="content",
        )
        _insert_new(engine.repo, new_entity)

        consolidator = EntityConsolidator(
            memory_db, _StubJudge(response=None), engine.config,
        )
        decisions = {new_entity.id: ("NOOP", old_id, "stubbed")}

        stats = consolidator._apply([new_entity], decisions)

        assert stats.noop == 1
        assert stats.added == 0
        assert engine.repo.get_entity(new_entity.id)["archived_at"] is not None


class TestFailOpen:
    def test_malformed_judge_output_keeps_everything_as_add(
        self, memory_db: Database, caplog,
    ) -> None:
        engine = _make_engine(memory_db)
        old_id = _insert_entity(
            engine.repo, engine.project_id, "fact",
            "Queue backend is Redis", "used for background jobs",
        )
        engine.repo.set_citation_counts({old_id: (3, "2026-01-01T00:00:00")})
        new_entity = Entity(
            project_id=engine.project_id, type="fact",
            title="Queue backend is Redis", content="used for background jobs",
        )
        _insert_new(engine.repo, new_entity)
        engine.repo.set_citation_counts({
            new_entity.id: (2, "2026-01-02T00:00:00"),
        })

        judge = _StubJudge(response="not valid json at all")
        consolidator = EntityConsolidator(memory_db, judge, engine.config)

        with caplog.at_level(logging.WARNING):
            stats = consolidator.consolidate(engine.project_id, [new_entity])

        assert stats.added == 1
        assert stats.updated == 0
        assert stats.noop == 0
        assert stats.transfer_attempts == 0
        assert stats.judge_failed is True
        assert any(
            "malformed" in r.message.lower() or "fail-open" in r.message.lower()
            for r in caplog.records
        )

        # Nothing destroyed or altered on either side -- including
        # citation credit: fail-open must mean zero mutations, and that
        # includes zero citation transfers.
        old_row = engine.repo.get_entity(old_id)
        assert old_row["stale"] == 0
        assert old_row["archived_at"] is None
        assert old_row["cited_count"] == 3
        new_row = engine.repo.get_entity(new_entity.id)
        assert new_row["archived_at"] is None
        assert new_row["cited_count"] == 2

    def test_absent_judge_response_keeps_everything_as_add(
        self, memory_db: Database,
    ) -> None:
        engine = _make_engine(memory_db)
        old_id = _insert_entity(
            engine.repo, engine.project_id, "fact",
            "Queue backend is Redis", "used for background jobs",
        )
        engine.repo.set_citation_counts({old_id: (5, "2026-01-01T00:00:00")})
        new_entity = Entity(
            project_id=engine.project_id, type="fact",
            title="Queue backend is Redis", content="used for background jobs",
        )
        _insert_new(engine.repo, new_entity)

        judge = _StubJudge(response=None)
        consolidator = EntityConsolidator(memory_db, judge, engine.config)
        stats = consolidator.consolidate(engine.project_id, [new_entity])

        assert stats.added == 1
        assert stats.transfer_attempts == 0
        assert stats.judge_failed is True
        # Fail-open means zero mutations, including zero citation
        # transfers -- the cited old entity's count must be untouched.
        assert engine.repo.get_entity(old_id)["cited_count"] == 5

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

    def test_successful_run_logs_info_with_counts(
        self, memory_db: Database, caplog,
    ) -> None:
        engine = _make_engine(memory_db)
        old_id = _insert_entity(
            engine.repo, engine.project_id, "fact", "Auth uses JWT", "RS256",
        )
        new_entity = Entity(
            project_id=engine.project_id, type="fact",
            title="Auth uses JWT", content="RS256",
        )
        _insert_new(engine.repo, new_entity)
        judge = _StubJudge(_judge_response("UPDATE", new_entity.id, old_id))
        consolidator = EntityConsolidator(memory_db, judge, engine.config)

        with caplog.at_level(logging.INFO):
            stats = consolidator.consolidate(engine.project_id, [new_entity])

        assert stats.updated == 1
        assert any(
            "consolidation run" in r.message.lower()
            and "1 updated" in r.message.lower()
            for r in caplog.records
        )


class TestJudgeShapeValidation:
    """Malformed-shape variants the reviewer verified manually -- each
    must hit the standard fail-open path (judge_failed=True, everything
    ADD) rather than crash or silently misbehave."""

    def test_hallucinated_existing_id_fails_open(
        self, memory_db: Database,
    ) -> None:
        engine = _make_engine(memory_db)
        _insert_entity(
            engine.repo, engine.project_id, "fact", "Auth uses JWT", "RS256",
        )
        new_entity = Entity(
            project_id=engine.project_id, type="fact",
            title="Auth uses JWT", content="RS256",
        )
        _insert_new(engine.repo, new_entity)

        response = json.dumps([{
            "new_id": new_entity.id, "verdict": "UPDATE",
            "existing_id": "not-a-real-candidate-id",
        }])
        judge = _StubJudge(response=response)
        consolidator = EntityConsolidator(memory_db, judge, engine.config)
        stats = consolidator.consolidate(engine.project_id, [new_entity])

        assert stats.judge_failed is True
        assert stats.added == 1

    def test_duplicate_verdict_for_same_new_id_fails_open(
        self, memory_db: Database,
    ) -> None:
        engine = _make_engine(memory_db)
        old_id = _insert_entity(
            engine.repo, engine.project_id, "fact", "Auth uses JWT", "RS256",
        )
        new_entity = Entity(
            project_id=engine.project_id, type="fact",
            title="Auth uses JWT", content="RS256",
        )
        _insert_new(engine.repo, new_entity)

        response = json.dumps([
            {"new_id": new_entity.id, "verdict": "ADD", "existing_id": None},
            {"new_id": new_entity.id, "verdict": "UPDATE", "existing_id": old_id},
        ])
        judge = _StubJudge(response=response)
        consolidator = EntityConsolidator(memory_db, judge, engine.config)
        stats = consolidator.consolidate(engine.project_id, [new_entity])

        assert stats.judge_failed is True
        assert stats.added == 1
        assert engine.repo.get_entity(old_id)["stale"] == 0

    def test_unknown_verdict_fails_open(self, memory_db: Database) -> None:
        engine = _make_engine(memory_db)
        _insert_entity(
            engine.repo, engine.project_id, "fact", "Auth uses JWT", "RS256",
        )
        new_entity = Entity(
            project_id=engine.project_id, type="fact",
            title="Auth uses JWT", content="RS256",
        )
        _insert_new(engine.repo, new_entity)

        response = json.dumps([
            {"new_id": new_entity.id, "verdict": "MAYBE", "existing_id": None},
        ])
        judge = _StubJudge(response=response)
        consolidator = EntityConsolidator(memory_db, judge, engine.config)
        stats = consolidator.consolidate(engine.project_id, [new_entity])

        assert stats.judge_failed is True
        assert stats.added == 1

    def test_non_list_json_fails_open(self, memory_db: Database) -> None:
        engine = _make_engine(memory_db)
        _insert_entity(
            engine.repo, engine.project_id, "fact", "Auth uses JWT", "RS256",
        )
        new_entity = Entity(
            project_id=engine.project_id, type="fact",
            title="Auth uses JWT", content="RS256",
        )
        _insert_new(engine.repo, new_entity)

        # A JSON object, not the required array.
        response = json.dumps({"new_id": new_entity.id, "verdict": "ADD"})
        judge = _StubJudge(response=response)
        consolidator = EntityConsolidator(memory_db, judge, engine.config)
        stats = consolidator.consolidate(engine.project_id, [new_entity])

        assert stats.judge_failed is True
        assert stats.added == 1

    def test_list_valued_new_id_fails_open_without_crashing(
        self, memory_db: Database,
    ) -> None:
        """A non-string id would raise TypeError on a dict/set membership
        check (unhashable) if not guarded -- that must never bypass the
        fail-open path."""
        engine = _make_engine(memory_db)
        _insert_entity(
            engine.repo, engine.project_id, "fact", "Auth uses JWT", "RS256",
        )
        new_entity = Entity(
            project_id=engine.project_id, type="fact",
            title="Auth uses JWT", content="RS256",
        )
        _insert_new(engine.repo, new_entity)

        response = json.dumps([{
            "new_id": ["not", "a", "string"],
            "verdict": "ADD", "existing_id": None,
        }])
        judge = _StubJudge(response=response)
        consolidator = EntityConsolidator(memory_db, judge, engine.config)

        stats = consolidator.consolidate(engine.project_id, [new_entity])

        assert stats.judge_failed is True
        assert stats.added == 1

    def test_dict_valued_existing_id_fails_open_without_crashing(
        self, memory_db: Database,
    ) -> None:
        engine = _make_engine(memory_db)
        old_id = _insert_entity(
            engine.repo, engine.project_id, "fact", "Auth uses JWT", "RS256",
        )
        new_entity = Entity(
            project_id=engine.project_id, type="fact",
            title="Auth uses JWT", content="RS256",
        )
        _insert_new(engine.repo, new_entity)

        response = json.dumps([{
            "new_id": new_entity.id, "verdict": "UPDATE",
            "existing_id": {"nested": old_id},
        }])
        judge = _StubJudge(response=response)
        consolidator = EntityConsolidator(memory_db, judge, engine.config)

        stats = consolidator.consolidate(engine.project_id, [new_entity])

        assert stats.judge_failed is True
        assert stats.added == 1
        assert engine.repo.get_entity(old_id)["stale"] == 0


class TestSameBatchSiblingsNeverConsolidateEachOther:
    """Reviewer-reported critical: entities from the SAME extraction batch
    are inserted (and FTS-indexed) before consolidation runs, so without
    an explicit exclusion a batch sibling looks like a legitimate
    "existing" candidate. A judge mistakenly returning mutual NOOP would
    archive BOTH copies of the same fact (nothing survives); mutual UPDATE
    would create a supersede 2-cycle. Both stub responses below are
    exactly what a judge WOULD return if it ever saw the siblings as each
    other's candidates -- the exclusion must make that unreachable."""

    def test_mutual_noop_leaves_both_batch_siblings_active(
        self, memory_db: Database,
    ) -> None:
        engine = _make_engine(memory_db)
        entity_a = Entity(
            project_id=engine.project_id, type="fact",
            title="Auth uses JWT tokens", content="signed with RS256",
        )
        entity_b = Entity(
            project_id=engine.project_id, type="fact",
            title="Auth uses JWT tokens", content="signed with RS256",
        )
        _insert_new(engine.repo, entity_a)
        _insert_new(engine.repo, entity_b)

        response = json.dumps([
            {"new_id": entity_a.id, "verdict": "NOOP", "existing_id": entity_b.id},
            {"new_id": entity_b.id, "verdict": "NOOP", "existing_id": entity_a.id},
        ])
        judge = _StubJudge(response=response)
        consolidator = EntityConsolidator(memory_db, judge, engine.config)

        stats = consolidator.consolidate(
            engine.project_id, [entity_a, entity_b],
        )

        # Siblings never qualify as each other's candidates, so the judge
        # is never even called.
        assert judge.calls == []
        assert stats.added == 2
        assert stats.noop == 0

        row_a = engine.repo.get_entity(entity_a.id)
        row_b = engine.repo.get_entity(entity_b.id)
        assert row_a["archived_at"] is None
        assert row_b["archived_at"] is None
        assert row_a["stale"] == 0
        assert row_b["stale"] == 0

    def test_mutual_update_creates_no_supersede_cycle(
        self, memory_db: Database,
    ) -> None:
        engine = _make_engine(memory_db)
        entity_a = Entity(
            project_id=engine.project_id, type="fact",
            title="DB is SQLite", content="WAL mode",
        )
        entity_b = Entity(
            project_id=engine.project_id, type="fact",
            title="DB is SQLite", content="WAL mode",
        )
        _insert_new(engine.repo, entity_a)
        _insert_new(engine.repo, entity_b)

        response = json.dumps([
            {"new_id": entity_a.id, "verdict": "UPDATE", "existing_id": entity_b.id},
            {"new_id": entity_b.id, "verdict": "UPDATE", "existing_id": entity_a.id},
        ])
        judge = _StubJudge(response=response)
        consolidator = EntityConsolidator(memory_db, judge, engine.config)

        stats = consolidator.consolidate(
            engine.project_id, [entity_a, entity_b],
        )

        assert judge.calls == []
        assert stats.added == 2
        assert stats.updated == 0

        row_a = engine.repo.get_entity(entity_a.id)
        row_b = engine.repo.get_entity(entity_b.id)
        assert row_a["stale"] == 0
        assert row_b["stale"] == 0
        assert row_a["superseded_by"] is None
        assert row_b["superseded_by"] is None

    def test_belt_and_braces_rejects_existing_id_naming_a_batch_sibling(
        self, memory_db: Database,
    ) -> None:
        """Second line of defense: even if a candidate lookup somehow let a
        batch sibling through, _parse must still reject an existing_id
        that names a batch entity, rather than apply it."""
        engine = _make_engine(memory_db)
        # A real pre-existing candidate so entity_a legitimately qualifies.
        _insert_entity(
            engine.repo, engine.project_id, "fact", "Auth uses JWT", "RS256",
        )
        entity_a = Entity(
            project_id=engine.project_id, type="fact",
            title="Auth uses JWT", content="RS256",
        )
        entity_b = Entity(
            project_id=engine.project_id, type="fact",
            title="Auth uses JWT", content="RS256",
        )
        _insert_new(engine.repo, entity_a)
        _insert_new(engine.repo, entity_b)

        # entity_a legitimately qualifies against old_id, but the judge
        # (mis)names entity_b -- a batch sibling -- as the existing_id.
        response = json.dumps([
            {"new_id": entity_a.id, "verdict": "NOOP", "existing_id": entity_b.id},
        ])
        judge = _StubJudge(response=response)
        consolidator = EntityConsolidator(memory_db, judge, engine.config)

        stats = consolidator.consolidate(
            engine.project_id, [entity_a, entity_b],
        )

        assert stats.judge_failed is True
        assert stats.added == 2
        row_a = engine.repo.get_entity(entity_a.id)
        row_b = engine.repo.get_entity(entity_b.id)
        assert row_a["archived_at"] is None
        assert row_b["archived_at"] is None


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


class TestMixedVerdictsInOneBatch:
    def test_update_and_contradicts_in_same_batch_are_independent(
        self, memory_db: Database,
    ) -> None:
        """Adding CONTRADICTS must not change how UPDATE (or NOOP/ADD)
        already behave -- each new entity's verdict is applied purely on
        its own terms."""
        engine = _make_engine(memory_db)
        old_a = _insert_entity(
            engine.repo, engine.project_id, "decision",
            "Use Redis for caching", "Chose Redis because it is fast",
        )
        old_b = _insert_entity(
            engine.repo, engine.project_id, "fact",
            "Auth uses JWT", "RS256",
        )
        new_a = Entity(
            project_id=engine.project_id, type="decision",
            title="Use Redis for caching",
            content="Chose Redis with a 1-hour TTL after benchmarking",
        )
        new_b = Entity(
            project_id=engine.project_id, type="fact",
            title="Auth uses JWT", content="Actually mTLS now",
        )
        _insert_new(engine.repo, new_a)
        _insert_new(engine.repo, new_b)

        response = json.dumps([
            {"new_id": new_a.id, "verdict": "UPDATE", "existing_id": old_a},
            {"new_id": new_b.id, "verdict": "CONTRADICTS", "existing_id": old_b},
        ])
        judge = _StubJudge(response=response)
        consolidator = EntityConsolidator(memory_db, judge, engine.config)

        stats = consolidator.consolidate(engine.project_id, [new_a, new_b])

        assert stats.updated == 1
        assert stats.contradicted == 1

        old_a_row = engine.repo.get_entity(old_a)
        assert old_a_row["staleness_reason"] == "consolidated"
        assert old_a_row["invalidated_at"] is None

        old_b_row = engine.repo.get_entity(old_b)
        assert old_b_row["staleness_reason"] == "contradicted"
        assert old_b_row["invalidated_at"] is not None


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

    def test_dim_mismatched_corpus_falls_back_to_fts(
        self, memory_db: Database,
    ) -> None:
        """Every stored vector has a different dimensionality than the
        query (e.g. an old embedding model's leftovers) -- rank_by_similarity
        skips all of them, and that must be treated as "vector data
        unusable for this candidate set" (fall back to FTS), not as
        "found nothing similar" (which would wrongly suppress FTS)."""
        engine = _make_engine(memory_db)
        old_id = _insert_entity(
            engine.repo, engine.project_id, "fact",
            "Auth uses JWT tokens", "signed with RS256",
        )
        model_key = embedding_model_key(engine.config)
        # Stored at 5 dims; the (stub) embedder below returns 3 dims for
        # the query -- every candidate gets skipped by rank_by_similarity.
        engine.repo.upsert_embedding(
            old_id, model_key, 5, pack_vector([1.0, 0.0, 0.0, 0.0, 0.0]),
        )

        new_entity = Entity(
            project_id=engine.project_id, type="fact",
            title="Auth uses JWT tokens", content="signed with RS256",
        )
        _insert_new(engine.repo, new_entity)

        judge = _StubJudge(_judge_response("UPDATE", new_entity.id, old_id))
        embedder = _StubEmbedder([1.0, 0.0, 0.0])  # 3 dims -- mismatched
        consolidator = EntityConsolidator(
            memory_db, judge, engine.config, embedder=embedder,
        )

        stats = consolidator.consolidate(engine.project_id, [new_entity])

        # Fell back to FTS, which matches on the identical title -- the
        # judge call goes through and the UPDATE applies.
        assert len(judge.calls) == 1
        assert stats.updated == 1


class TestBatchedEmbedding:
    def test_one_embed_call_for_the_whole_batch(
        self, memory_db: Database,
    ) -> None:
        engine = _make_engine(memory_db)
        old_a = _insert_entity(
            engine.repo, engine.project_id, "fact", "Auth uses JWT", "RS256",
        )
        old_b = _insert_entity(
            engine.repo, engine.project_id, "fact", "DB is SQLite", "WAL mode",
        )
        model_key = embedding_model_key(engine.config)
        vector = [1.0, 0.0, 0.0]
        engine.repo.upsert_embedding(old_a, model_key, 3, pack_vector(vector))
        engine.repo.upsert_embedding(old_b, model_key, 3, pack_vector(vector))

        new_a = Entity(
            project_id=engine.project_id, type="fact",
            title="Different wording a", content="Different wording a",
        )
        new_b = Entity(
            project_id=engine.project_id, type="fact",
            title="Different wording b", content="Different wording b",
        )
        _insert_new(engine.repo, new_a)
        _insert_new(engine.repo, new_b)

        response = json.dumps([
            {"new_id": new_a.id, "verdict": "ADD", "existing_id": None},
            {"new_id": new_b.id, "verdict": "ADD", "existing_id": None},
        ])
        judge = _StubJudge(response=response)
        embedder = _SpyEmbedder(vector)
        consolidator = EntityConsolidator(
            memory_db, judge, engine.config, embedder=embedder,
        )

        consolidator.consolidate(engine.project_id, [new_a, new_b])

        assert len(embedder.calls) == 1
        assert len(embedder.calls[0]) == 2


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

    def test_default_config_skips_entirely(self, memory_db: Database) -> None:
        """The out-of-the-box Config() -- not the test helper's
        explicitly-enabled one -- must leave consolidation off."""
        config = Config(sensitive_data={"enabled": False, "llm_scan": False})
        engine = MemoryEngine(memory_db, config)
        _insert_entity(
            engine.repo, engine.project_id, "fact", "Auth uses JWT", "RS256",
        )
        new_entity = Entity(
            project_id=engine.project_id, type="fact",
            title="Auth uses JWT", content="RS256",
        )
        judge = _StubJudge(_judge_response("NOOP", new_entity.id, "whatever"))
        consolidator = EntityConsolidator(memory_db, judge, config)

        stats = consolidator.consolidate(engine.project_id, [new_entity])

        assert judge.calls == []
        assert stats == stats.__class__()


class TestExtractionIntegration:
    """Consolidation runs at extraction-batch completion, scoped to the
    EntityExtractor path only (see task-2-report.md for why the other two
    entity-creation paths from Task 1 are out of scope for this task)."""

    def test_consolidate_called_after_extraction_with_config(
        self, memory_db: Database,
    ) -> None:
        config = Config(
            sensitive_data={"enabled": False, "llm_scan": False},
            consolidation={"enabled": True},
        )
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

    def test_default_disabled_config_skips_consolidation(
        self, memory_db: Database,
    ) -> None:
        """A plain Config() (consolidation off by default) must never
        even construct/call the consolidator."""
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
        mock_consolidate.assert_not_called()

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
        config = Config(
            sensitive_data={"enabled": False, "llm_scan": False},
            consolidation={"enabled": True},
        )
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


def _snapshot(repo: Repository, *entity_ids: str) -> list[dict[str, Any]]:
    """Full row state for a set of entities -- used to assert dry-run
    writes nothing, byte-for-byte."""
    return [repo.get_entity(eid) for eid in entity_ids]


def _consolidation_log_row_count(db: Database, project_id: str) -> int:
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM consolidation_log WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        return row["n"]
    finally:
        conn.close()


class TestDryRun:
    """Task 4: the shadow-mode calibration harness. ``dry_run=True`` must
    run through the identical candidate-selection/judging/apply-planning
    code the live path uses, and must never write to the DB."""

    def test_noop_dry_run_writes_nothing(self, memory_db: Database) -> None:
        engine = _make_engine(memory_db)
        old_id = _insert_entity(
            engine.repo, engine.project_id, "fact",
            "Auth uses JWT", "RS256 tokens",
        )
        new_entity = Entity(
            project_id=engine.project_id, type="fact",
            title="Auth uses JWT", content="RS256 tokens",
        )
        _insert_new(engine.repo, new_entity)
        engine.repo.set_citation_counts({old_id: (3, "2026-01-01T00:00:00")})

        before = _snapshot(engine.repo, old_id, new_entity.id)
        before_log_count = _consolidation_log_row_count(memory_db, engine.project_id)

        judge = _StubJudge(_judge_response("NOOP", new_entity.id, old_id))
        consolidator = EntityConsolidator(memory_db, judge, engine.config)
        stats = consolidator.consolidate(
            engine.project_id, [new_entity], dry_run=True,
        )

        after = _snapshot(engine.repo, old_id, new_entity.id)
        assert after == before
        assert (
            _consolidation_log_row_count(memory_db, engine.project_id)
            == before_log_count
        )
        # Reports what it WOULD do, without touching stats.transfer_attempts
        # (no real transfer happened).
        assert stats.noop == 1
        assert stats.transfer_attempts == 0

    def test_update_dry_run_writes_nothing(self, memory_db: Database) -> None:
        engine = _make_engine(memory_db)
        old_id = _insert_entity(
            engine.repo, engine.project_id, "fact",
            "Auth uses JWT", "RS256",
        )
        new_entity = Entity(
            project_id=engine.project_id, type="fact",
            title="Auth uses JWT", content="RS256, rotated quarterly",
        )
        _insert_new(engine.repo, new_entity)

        before = _snapshot(engine.repo, old_id, new_entity.id)

        judge = _StubJudge(_judge_response("UPDATE", new_entity.id, old_id))
        consolidator = EntityConsolidator(memory_db, judge, engine.config)
        stats = consolidator.consolidate(
            engine.project_id, [new_entity], dry_run=True,
        )

        after = _snapshot(engine.repo, old_id, new_entity.id)
        assert after == before
        assert stats.updated == 1
        # In particular: superseded_by/stale must still be unset -- the
        # exact thing a calibration run must never assert falsely.
        assert after[0]["stale"] == 0
        assert after[0]["superseded_by"] is None

    def test_contradicts_dry_run_writes_nothing(self, memory_db: Database) -> None:
        engine = _make_engine(memory_db)
        old_id = _insert_entity(
            engine.repo, engine.project_id, "fact",
            "Auth uses JWT", "RS256",
        )
        new_entity = Entity(
            project_id=engine.project_id, type="fact",
            title="Auth uses JWT", content="Actually mTLS now",
        )
        _insert_new(engine.repo, new_entity)

        before = _snapshot(engine.repo, old_id, new_entity.id)

        judge = _StubJudge(_judge_response("CONTRADICTS", new_entity.id, old_id))
        consolidator = EntityConsolidator(memory_db, judge, engine.config)
        stats = consolidator.consolidate(
            engine.project_id, [new_entity], dry_run=True,
        )

        after = _snapshot(engine.repo, old_id, new_entity.id)
        assert after == before
        assert stats.contradicted == 1

    def test_dry_run_works_even_when_consolidation_disabled(
        self, memory_db: Database,
    ) -> None:
        """The whole point of shadow mode: a project calibrating a
        threshold has consolidation.enabled=False. dry_run must bypass
        that gate -- it is the ONLY way to see real behaviour before
        flipping the switch."""
        config = Config(sensitive_data={"enabled": False, "llm_scan": False})
        assert config.consolidation.enabled is False
        engine = MemoryEngine(memory_db, config)
        old_id = _insert_entity(
            engine.repo, engine.project_id, "fact",
            "Auth uses JWT", "RS256",
        )
        new_entity = Entity(
            project_id=engine.project_id, type="fact",
            title="Auth uses JWT", content="RS256",
        )
        _insert_new(engine.repo, new_entity)

        judge = _StubJudge(_judge_response("NOOP", new_entity.id, old_id))
        consolidator = EntityConsolidator(memory_db, judge, config)
        stats = consolidator.consolidate(
            engine.project_id, [new_entity], dry_run=True,
        )

        assert judge.calls != []
        assert stats.noop == 1
        assert engine.repo.get_entity(new_entity.id)["archived_at"] is None

    def test_dry_run_decision_carries_similarity_and_reason(
        self, memory_db: Database,
    ) -> None:
        engine = _make_engine(memory_db)
        old_id = _insert_entity(
            engine.repo, engine.project_id, "fact",
            "Auth uses JWT", "RS256",
        )
        new_entity = Entity(
            project_id=engine.project_id, type="fact",
            title="Auth uses JWT", content="RS256",
        )
        _insert_new(engine.repo, new_entity)

        judge = _StubJudge(json.dumps([{
            "new_id": new_entity.id, "verdict": "NOOP", "existing_id": old_id,
            "reason": "identical fact, no new information",
        }]))
        consolidator = EntityConsolidator(memory_db, judge, engine.config)
        stats = consolidator.consolidate(
            engine.project_id, [new_entity], dry_run=True,
        )

        assert stats.decisions is not None
        [decision] = stats.decisions
        assert decision.entity_id == new_entity.id
        assert decision.verdict == "NOOP"
        assert decision.existing_id == old_id
        assert decision.reason == "identical fact, no new information"
        assert decision.gate_similarity is not None
        assert 0.0 < decision.gate_similarity <= 1.0
        assert decision.matched_similarity is not None
        assert 0.0 < decision.matched_similarity <= 1.0

    def test_dry_run_below_threshold_entity_reports_add_with_near_miss_similarity(
        self, memory_db: Database,
    ) -> None:
        engine = _make_engine(memory_db)
        _insert_entity(
            engine.repo, engine.project_id, "fact",
            "Auth uses JWT tokens", "RS256",
        )
        new_entity = Entity(
            project_id=engine.project_id, type="fact",
            # Shares "Auth uses" with the candidate above (~0.67
            # SequenceMatcher ratio) -- an FTS hit exists, it's just
            # below the threshold below, unlike a totally unrelated
            # title, which would find no FTS candidate at all.
            title="Auth uses session cookies", content="cookie-backed",
        )
        config = engine.config.model_copy(deep=True)
        config.consolidation.threshold = 0.99  # unreachable
        judge = _StubJudge(response=None)
        consolidator = EntityConsolidator(memory_db, judge, config)

        stats = consolidator.consolidate(
            engine.project_id, [new_entity], dry_run=True,
        )

        assert judge.calls == []
        assert stats.added == 1
        assert stats.decisions is not None
        [decision] = stats.decisions
        assert decision.verdict == "ADD"
        # Distinct from a genuine judge ADD or a judge failure -- this
        # entity never reached the judge at all.
        assert decision.reason == "below_threshold"
        # Never sent to the judge, but the near-miss gate score is still
        # surfaced -- this is exactly the data a threshold sweep needs.
        assert decision.gate_similarity is not None
        assert decision.matched_similarity is None

    def test_dry_run_threshold_override_changes_qualifying_set(
        self, memory_db: Database,
    ) -> None:
        """Same corpus, two thresholds: a loose one routes the pair to
        the judge, a strict one doesn't -- proving --threshold actually
        changes the candidate gate, not just cosmetics."""
        engine = _make_engine(memory_db)
        _insert_entity(
            engine.repo, engine.project_id, "fact",
            "Auth uses JWT tokens", "RS256",
        )
        new_entity = Entity(
            project_id=engine.project_id, type="fact",
            title="Auth uses session cookies", content="cookie-backed",
        )

        strict = engine.config.model_copy(deep=True)
        strict.consolidation.threshold = 0.99
        judge_strict = _StubJudge(response=None)
        strict_stats = EntityConsolidator(
            memory_db, judge_strict, strict,
        ).consolidate(engine.project_id, [new_entity], dry_run=True)
        assert judge_strict.calls == []
        assert strict_stats.decisions[0].verdict == "ADD"

        loose = engine.config.model_copy(deep=True)
        loose.consolidation.threshold = 0.1
        judge_loose = _StubJudge(_judge_response("ADD", new_entity.id, None))
        loose_stats = EntityConsolidator(
            memory_db, judge_loose, loose,
        ).consolidate(engine.project_id, [new_entity], dry_run=True)
        assert len(judge_loose.calls) == 1
        assert loose_stats.decisions[0].gate_similarity is not None

    def test_judge_failure_in_dry_run_reports_add_never_a_would_archive(
        self, memory_db: Database,
    ) -> None:
        engine = _make_engine(memory_db)
        old_id = _insert_entity(
            engine.repo, engine.project_id, "fact",
            "Auth uses JWT", "RS256",
        )
        new_entity = Entity(
            project_id=engine.project_id, type="fact",
            title="Auth uses JWT", content="RS256",
        )
        _insert_new(engine.repo, new_entity)
        before = _snapshot(engine.repo, old_id, new_entity.id)

        judge = _StubJudge(response="not valid json")
        consolidator = EntityConsolidator(memory_db, judge, engine.config)
        stats = consolidator.consolidate(
            engine.project_id, [new_entity], dry_run=True,
        )

        assert stats.judge_failed is True
        assert stats.updated == 0
        assert stats.noop == 0
        assert stats.contradicted == 0
        assert stats.added == 1
        assert stats.decisions is not None
        [decision] = stats.decisions
        assert decision.verdict == "ADD"
        assert decision.reason == "judge_failed"
        assert _snapshot(engine.repo, old_id, new_entity.id) == before

    def test_duplicate_existing_id_collision_resolved_same_as_live(
        self, memory_db: Database,
    ) -> None:
        """Dry-run must reuse `_apply`'s collision-resolution logic, not
        just its candidate-selection/judging half -- otherwise its
        counts would overstate what a live run would actually do."""
        engine = _make_engine(memory_db)
        old_id = _insert_entity(
            engine.repo, engine.project_id, "fact", "dup title", "content",
        )
        new_a = Entity(
            project_id=engine.project_id, type="fact",
            title="dup title", content="content v2",
        )
        new_b = Entity(
            project_id=engine.project_id, type="fact",
            title="dup title", content="content v3",
        )
        _insert_new(engine.repo, new_a)
        _insert_new(engine.repo, new_b)

        response = json.dumps([
            {"new_id": new_a.id, "verdict": "UPDATE", "existing_id": old_id,
             "reason": "refines"},
            {"new_id": new_b.id, "verdict": "UPDATE", "existing_id": old_id,
             "reason": "also refines"},
        ])
        judge = _StubJudge(response=response)
        consolidator = EntityConsolidator(memory_db, judge, engine.config)
        stats = consolidator.consolidate(
            engine.project_id, [new_a, new_b], dry_run=True,
        )

        # Exactly one of the two duplicate claims wins -- same guarantee
        # Task 2 established for the live path.
        assert stats.updated == 1
        assert stats.added == 1
        assert engine.repo.get_entity(old_id)["stale"] == 0

    def test_dry_run_and_live_path_share_the_same_apply_method(
        self, memory_db: Database,
    ) -> None:
        """Structural assertion for Task 4's core requirement: the
        dry-run harness must be the SAME candidate-selection/judging/
        apply-planning code as the live path, not a parallel
        reimplementation. Spy on the real, bound `_apply`/`_find_similar`
        /`_parse` methods (wraps=..., so they still run for real) and
        confirm dry_run=True routes straight through them, with only the
        `apply` flag differing from a live call."""
        engine = _make_engine(memory_db)
        old_id = _insert_entity(
            engine.repo, engine.project_id, "fact",
            "Auth uses JWT", "RS256",
        )
        new_entity = Entity(
            project_id=engine.project_id, type="fact",
            title="Auth uses JWT", content="RS256",
        )
        _insert_new(engine.repo, new_entity)

        judge = _StubJudge(_judge_response("NOOP", new_entity.id, old_id))
        consolidator = EntityConsolidator(memory_db, judge, engine.config)

        with (
            patch.object(
                consolidator, "_find_similar",
                wraps=consolidator._find_similar,
            ) as spy_find,
            patch.object(
                consolidator, "_parse", wraps=consolidator._parse,
            ) as spy_parse,
            patch.object(
                consolidator, "_apply", wraps=consolidator._apply,
            ) as spy_apply,
        ):
            stats = consolidator.consolidate(
                engine.project_id, [new_entity], dry_run=True,
            )

        assert spy_find.called
        assert spy_parse.called
        spy_apply.assert_called_once()
        assert spy_apply.call_args.kwargs.get("apply") is False
        # And it genuinely ran (wraps=..., not a stub) -- the dry-run
        # verdict is real, not fabricated by the spy.
        assert stats.noop == 1
        assert engine.repo.get_entity(new_entity.id)["archived_at"] is None

        # Same corpus, live call: the identical `_apply` method, only
        # `apply` flips to True (its default) and it actually writes.
        judge2 = _StubJudge(_judge_response("NOOP", new_entity.id, old_id))
        consolidator2 = EntityConsolidator(memory_db, judge2, engine.config)
        with patch.object(
            consolidator2, "_apply", wraps=consolidator2._apply,
        ) as spy_apply_live:
            consolidator2.consolidate(engine.project_id, [new_entity])
        assert spy_apply_live.call_args.kwargs.get("apply") is True
        assert engine.repo.get_entity(new_entity.id)["archived_at"] is not None

    def test_no_parallel_apply_reimplementation_exists(self) -> None:
        """Belt-and-braces on the structural requirement above: `_apply`
        is the single method with an `apply` flag: no second
        dry-run-flavoured "apply the plan" method exists on the class
        for a dry run to have drifted into using instead."""
        import inspect

        sig = inspect.signature(EntityConsolidator._apply)
        assert "apply" in sig.parameters

        apply_like = [
            name for name in dir(EntityConsolidator)
            if name != "_apply" and "apply" in name.lower()
        ]
        assert apply_like == []

    def test_matched_similarity_differs_from_gate_similarity_for_lower_ranked_pick(
        self, memory_db: Database,
    ) -> None:
        """R3: the judge may name ANY top-K candidate, not just the
        highest-scoring one -- gate_similarity (what the threshold
        compared) and matched_similarity (the named candidate's own
        score) must be reported separately, never one silently
        substituted for the other."""
        engine = _make_engine(memory_db)
        candidate_a = _insert_entity(
            engine.repo, engine.project_id, "fact", "Top match", "content A",
        )
        candidate_b = _insert_entity(
            engine.repo, engine.project_id, "fact", "Second match", "content B",
        )
        model_key = embedding_model_key(engine.config)
        engine.repo.upsert_embedding(
            candidate_a, model_key, 2, pack_vector([1.0, 0.0]),
        )
        engine.repo.upsert_embedding(
            candidate_b, model_key, 2, pack_vector([0.6, 0.8]),
        )
        new_entity = Entity(
            project_id=engine.project_id, type="fact",
            title="New", content="new content",
        )
        _insert_new(engine.repo, new_entity)

        # Judge names candidate_b -- the LOWER-scoring (0.6) candidate,
        # not candidate_a (1.0, the one that set the gate).
        judge = _StubJudge(_judge_response("NOOP", new_entity.id, candidate_b))
        embedder = _StubEmbedder([1.0, 0.0])
        consolidator = EntityConsolidator(
            memory_db, judge, engine.config, embedder=embedder,
        )

        stats = consolidator.consolidate(
            engine.project_id, [new_entity], dry_run=True,
        )

        [decision] = stats.decisions
        assert decision.existing_id == candidate_b
        assert round(decision.gate_similarity, 3) == 1.0
        assert round(decision.matched_similarity, 3) == 0.6
        assert decision.gate_similarity != decision.matched_similarity

    def test_three_way_add_disaggregation_in_one_batch(
        self, memory_db: Database,
    ) -> None:
        """R4: a judged ADD, a never-asked (below threshold) entity, and
        (separately, below) a judge-failure must all be distinguishable
        via `reason` -- conflating them into one "ADD" bucket hides the
        single most decision-relevant split for calibration."""
        engine = _make_engine(memory_db)
        # judged_add's title tokens are a SUBSET of the candidate's, so
        # the FTS AND-match finds the candidate directly rather than
        # relying on the OR-retry fallback, which only fires when the
        # AND query matches nothing at all -- since judged_add is
        # itself a persisted row here, an AND query over its own title
        # always matches itself trivially and would otherwise mask
        # whether the candidate was found too (see the CLI test suite's
        # note on this same FTS quirk).
        _insert_entity(
            engine.repo, engine.project_id, "fact",
            "Auth uses JWT tokens for sessions", "RS256, rotated quarterly",
        )
        judged_add = Entity(
            project_id=engine.project_id, type="fact",
            title="Auth uses JWT tokens", content="short form",
        )
        never_asked = Entity(
            project_id=engine.project_id, type="fact",
            title="Completely unrelated turtles", content="nothing shared",
        )
        _insert_new(engine.repo, judged_add)
        _insert_new(engine.repo, never_asked)

        response = json.dumps([{
            "new_id": judged_add.id, "verdict": "ADD", "existing_id": None,
            "reason": "related but genuinely distinct",
        }])
        judge = _StubJudge(response=response)
        config = engine.config.model_copy(deep=True)
        config.consolidation.threshold = 0.5
        consolidator = EntityConsolidator(memory_db, judge, config)

        stats = consolidator.consolidate(
            engine.project_id, [judged_add, never_asked], dry_run=True,
        )

        by_id = {d.entity_id: d for d in stats.decisions}
        assert by_id[judged_add.id].verdict == "ADD"
        assert by_id[judged_add.id].reason == "related but genuinely distinct"
        assert by_id[never_asked.id].verdict == "ADD"
        assert by_id[never_asked.id].reason == "below_threshold"
        # The two ADDs are NOT the same population.
        assert by_id[judged_add.id].reason != by_id[never_asked.id].reason

    def test_judge_transport_error_fails_open_instead_of_raising(
        self, memory_db: Database, caplog,
    ) -> None:
        """R6: a transient transport error from the judge call (a bad
        response body, a connection hiccup any real client can leak)
        must fail open exactly like a malformed/absent response, not
        propagate and abort the caller's sweep."""
        import json as jsonlib

        engine = _make_engine(memory_db)
        old_id = _insert_entity(
            engine.repo, engine.project_id, "fact",
            "Auth uses JWT", "RS256",
        )
        new_entity = Entity(
            project_id=engine.project_id, type="fact",
            title="Auth uses JWT", content="RS256",
        )
        _insert_new(engine.repo, new_entity)
        before = _snapshot(engine.repo, old_id, new_entity.id)

        class _RaisingJudge:
            def extract(self, prompt: str) -> str | None:
                raise jsonlib.JSONDecodeError("bad body", "<html>", 0)

        consolidator = EntityConsolidator(memory_db, _RaisingJudge(), engine.config)

        with caplog.at_level(logging.WARNING):
            stats = consolidator.consolidate(
                engine.project_id, [new_entity], dry_run=True,
            )

        assert stats.judge_failed is True
        assert stats.updated == 0
        assert stats.noop == 0
        assert stats.contradicted == 0
        assert stats.added == 1
        [decision] = stats.decisions
        assert decision.reason == "judge_failed"
        assert _snapshot(engine.repo, old_id, new_entity.id) == before
        assert any(
            "JSONDecodeError" in r.message or "fail-open" in r.message.lower()
            for r in caplog.records
        )

    def test_judge_httpx_error_also_fails_open(self, memory_db: Database) -> None:
        """Same guarantee for the other narrow exception type -- an
        httpx transport error (timeout/connect/status) raised instead of
        being swallowed by the client's own internal handling."""
        import httpx

        engine = _make_engine(memory_db)
        _insert_entity(
            engine.repo, engine.project_id, "fact", "Auth uses JWT", "RS256",
        )
        new_entity = Entity(
            project_id=engine.project_id, type="fact",
            title="Auth uses JWT", content="RS256",
        )
        _insert_new(engine.repo, new_entity)

        class _TimingOutJudge:
            def extract(self, prompt: str) -> str | None:
                raise httpx.ReadTimeout("timed out")

        consolidator = EntityConsolidator(
            memory_db, _TimingOutJudge(), engine.config,
        )
        stats = consolidator.consolidate(
            engine.project_id, [new_entity], dry_run=True,
        )

        assert stats.judge_failed is True
        assert stats.added == 1
