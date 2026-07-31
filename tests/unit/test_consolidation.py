"""Tests for LLM-routed consolidation (ADD/UPDATE/NOOP)."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

from callmem.core.consolidation import EntityConsolidator
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
        self, memory_db: Database,
    ) -> None:
        """Two new entities both CONTRADICT the same existing entity.
        Only the first invalidation actually happens (mark_stale's
        WHERE stale=0 guard); the second must not be double-counted."""
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

        stats = consolidator.consolidate(
            engine.project_id, [entity_a, entity_b],
        )

        # Only one actual invalidation happened -- the second CONTRADICTS
        # found the target already stale and is a checked no-op.
        assert stats.contradicted == 1
        assert stats.added == 1

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
            "stats": (stats.updated, stats.noop, stats.added),
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
            "stats": (1, 1, 0),
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
        self, memory_db: Database,
    ) -> None:
        """Two different new entities both UPDATE the SAME existing_id.
        Only the first supersession actually happens in the DB (mark_stale's
        WHERE stale=0 guard); stats.updated must reflect that -- not double
        count -- and a later NOOP on the same target must redirect to
        whichever entity the DB actually recorded as the supersessor, not
        whichever entity happened to be processed last."""
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

        stats = consolidator.consolidate(
            engine.project_id, [entity_a, entity_b, entity_e],
        )

        # Exactly one supersession happened; the second UPDATE's target
        # was already stale, so it falls back to ADD rather than being
        # double-counted as updated.
        assert stats.updated == 1
        assert stats.added == 1
        assert stats.noop == 1

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
