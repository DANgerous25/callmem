"""Tests for Part A enhancements: task graph, model stats, eval,
context compilation, model registry, and rewind."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

import pytest

from callmem.core.repository import Repository
from callmem.models.model_registry import ModelRegistryEntry
from callmem.models.tasks import Task

if TYPE_CHECKING:
    from callmem.core.database import Database
    from callmem.core.engine import MemoryEngine


class TestTaskGraph:
    """A1: Subtask / Task-Graph Support."""

    def test_create_root_task(self, engine: MemoryEngine) -> None:
        task = engine.create_task(
            title="Implement feature X",
            task_type="coding",
            complexity_hint=5,
        )
        assert task["title"] == "Implement feature X"
        assert task["status"] == "pending"
        assert task["task_type"] == "coding"
        assert task["complexity_hint"] == 5
        assert task["parent_id"] is None

    def test_create_subtask(self, engine: MemoryEngine) -> None:
        root = engine.create_task(title="Root", task_type="coding")
        child = engine.create_task(
            title="Child", parent_id=root["id"],
        )
        assert child["parent_id"] == root["id"]
        assert child["status"] == "pending"

    def test_update_task_status(self, engine: MemoryEngine) -> None:
        task = engine.create_task(title="Test task")
        updated = engine.update_task(task["id"], {"status": "completed"})
        assert updated["status"] == "completed"
        assert updated["completed_at"] is not None

    def test_update_task_model_and_cost(self, engine: MemoryEngine) -> None:
        task = engine.create_task(title="Test task", task_type="coding")
        updated = engine.update_task(task["id"], {
            "model_assigned": "anthropic/claude-sonnet-4",
            "model_reason": "mid-tier coding task",
            "cost_usd": 0.015,
            "tokens_input": 3100,
            "tokens_output": 1200,
        })
        assert updated["model_assigned"] == "anthropic/claude-sonnet-4"
        assert updated["cost_usd"] == 0.015
        assert updated["tokens_input"] == 3100

    def test_list_tasks_by_status(self, engine: MemoryEngine) -> None:
        t1 = engine.create_task(title="Pending task")
        t2 = engine.create_task(title="Completed task")
        engine.update_task(t2["id"], {"status": "completed"})

        pending = engine.list_tasks(status="pending")
        completed = engine.list_tasks(status="completed")
        assert any(t["id"] == t1["id"] for t in pending)
        assert any(t["id"] == t2["id"] for t in completed)
        assert not any(t["id"] == t2["id"] for t in pending)

    def test_list_tasks_by_parent(self, engine: MemoryEngine) -> None:
        root = engine.create_task(title="Root")
        child1 = engine.create_task(title="Child 1", parent_id=root["id"])
        child2 = engine.create_task(title="Child 2", parent_id=root["id"])

        children = engine.list_tasks(parent_id=root["id"])
        assert len(children) == 2
        child_ids = {c["id"] for c in children}
        assert child1["id"] in child_ids
        assert child2["id"] in child_ids

    def test_get_task_tree(self, engine: MemoryEngine) -> None:
        root = engine.create_task(title="Root")
        child1 = engine.create_task(title="Child 1", parent_id=root["id"])
        grandchild = engine.create_task(title="Grandchild", parent_id=child1["id"])
        child2 = engine.create_task(title="Child 2", parent_id=root["id"])

        tree = engine.get_task_tree(root["id"])
        assert len(tree) == 4
        tree_ids = {t["id"] for t in tree}
        assert root["id"] in tree_ids
        assert child1["id"] in tree_ids
        assert grandchild["id"] in tree_ids
        assert child2["id"] in tree_ids

    def test_task_tree_empty(self, engine: MemoryEngine) -> None:
        tree = engine.get_task_tree("nonexistent-id")
        assert tree == []

    def test_update_nonexistent_task_raises(self, engine: MemoryEngine) -> None:
        with pytest.raises(ValueError, match="Task not found"):
            engine.update_task("nonexistent", {"status": "completed"})

    def test_completed_task_updates_model_stats(self, engine: MemoryEngine) -> None:
        task = engine.create_task(title="Test", task_type="coding")
        engine.update_task(task["id"], {
            "model_assigned": "test-model",
            "cost_usd": 0.01,
            "tokens_input": 100,
            "tokens_output": 50,
            "eval_score": 0.9,
        })
        engine.update_task(task["id"], {"status": "completed"})

        stats = engine.query_model_stats(model_name="test-model", task_type="coding")
        assert len(stats) == 1
        assert stats[0]["tasks_completed"] == 1
        assert stats[0]["total_cost_usd"] == 0.01

    def test_repository_insert_and_get_task(self, memory_db: Database) -> None:
        repo = Repository(memory_db)
        from callmem.models.projects import Project

        project = Project(name="test")
        repo.create_project(project)

        task = Task(
            project_id=project.id,
            title="Repository test",
            task_type="analysis",
        )
        repo.insert_task(task)
        fetched = repo.get_task(task.id)
        assert fetched is not None
        assert fetched.title == "Repository test"
        assert fetched.task_type == "analysis"


class TestModelStats:
    """A2: Model Performance Tracking."""

    def test_query_empty_stats(self, engine: MemoryEngine) -> None:
        stats = engine.query_model_stats()
        assert stats == []

    def test_compare_models_no_data(self, engine: MemoryEngine) -> None:
        comparison = engine.compare_models(["model-a", "model-b"], task_type="coding")
        assert len(comparison) == 2
        assert comparison[0]["tasks_completed"] == 0
        assert comparison[0]["note"] == "No observed data for this model."

    def test_upsert_model_stats_incremental(self, memory_db: Database) -> None:
        repo = Repository(memory_db)
        from callmem.models.projects import Project

        project = Project(name="test")
        repo.create_project(project)

        repo.upsert_model_stats(
            project.id, "model-a", "coding",
            completed_delta=1, eval_score=0.8, cost_delta=0.01,
            tokens_in_delta=100, tokens_out_delta=50,
        )
        stats = repo.get_model_stats(project.id, "model-a", "coding")
        assert stats is not None
        assert stats["tasks_completed"] == 1
        assert stats["avg_eval_score"] == 0.8

        repo.upsert_model_stats(
            project.id, "model-a", "coding",
            completed_delta=1, eval_score=0.9, cost_delta=0.02,
            tokens_in_delta=200, tokens_out_delta=100,
        )
        stats = repo.get_model_stats(project.id, "model-a", "coding")
        assert stats["tasks_completed"] == 2
        assert stats["total_cost_usd"] == 0.03
        assert stats["total_tokens_in"] == 300

    def test_upsert_model_stats_failed(self, memory_db: Database) -> None:
        repo = Repository(memory_db)
        from callmem.models.projects import Project

        project = Project(name="test")
        repo.create_project(project)

        repo.upsert_model_stats(
            project.id, "model-b", "coding", failed_delta=1,
        )
        stats = repo.get_model_stats(project.id, "model-b", "coding")
        assert stats["tasks_failed"] == 1
        assert stats["tasks_completed"] == 0

    def test_list_model_stats(self, memory_db: Database) -> None:
        repo = Repository(memory_db)
        from callmem.models.projects import Project

        project = Project(name="test")
        repo.create_project(project)

        repo.upsert_model_stats(project.id, "model-a", "coding", completed_delta=1)
        repo.upsert_model_stats(project.id, "model-b", "reasoning", completed_delta=1)
        repo.upsert_model_stats(project.id, "model-a", "reasoning", completed_delta=1)

        all_stats = repo.list_model_stats(project.id)
        assert len(all_stats) == 3

        model_a = repo.list_model_stats(project.id, model_name="model-a")
        assert len(model_a) == 2


class TestEval:
    """A3: Output Quality Scoring."""

    def test_eval_event(self, engine: MemoryEngine) -> None:
        engine.start_session()
        engine.ingest_one("note", "Test event")
        events = engine.get_events(limit=1)
        assert events

        result = engine.eval_event(
            events[0].id, 0.85, "Good implementation", "judge-model",
        )
        assert result["eval_score"] == 0.85
        assert result["eval_feedback"] == "Good implementation"
        assert result["eval_model"] == "judge-model"

    def test_eval_event_not_found(self, engine: MemoryEngine) -> None:
        with pytest.raises(ValueError, match="Event not found"):
            engine.eval_event("nonexistent", 0.5)

    def test_eval_event_invalid_score(self, engine: MemoryEngine) -> None:
        engine.start_session()
        engine.ingest_one("note", "Test event")
        events = engine.get_events(limit=1)
        with pytest.raises(ValueError, match="score must be between"):
            engine.eval_event(events[0].id, 1.5)

    def test_eval_entity(self, engine: MemoryEngine) -> None:
        from callmem.models.entities import Entity

        entity = Entity(
            project_id=engine.project_id,
            type="fact",
            title="Test entity",
            content="Test content",
        )
        conn = engine.db.connect()
        try:
            row = entity.to_row()
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

        result = engine.eval_entity(entity.id, 0.7, "Acceptable")
        assert result["eval_score"] == 0.7
        assert result["eval_feedback"] == "Acceptable"

    def test_eval_entity_not_found(self, engine: MemoryEngine) -> None:
        with pytest.raises(ValueError, match="Entity not found"):
            engine.eval_entity("nonexistent", 0.5)

    def test_eval_summary(self, engine: MemoryEngine) -> None:
        engine.start_session()
        engine.ingest_one("note", "Event 1")
        engine.ingest_one("note", "Event 2")
        events = engine.get_events(limit=2)
        engine.eval_event(events[0].id, 0.8, evaluator_model="judge-a")
        engine.eval_event(events[1].id, 0.6, evaluator_model="judge-a")

        summary = engine.eval_summary(model_name="judge-a")
        assert summary["events"]["count"] == 2
        assert summary["events"]["avg_score"] is not None
        assert 0.69 <= summary["events"]["avg_score"] <= 0.71

    def test_eval_summary_empty(self, engine: MemoryEngine) -> None:
        summary = engine.eval_summary()
        assert summary["events"]["count"] == 0
        assert summary["entities"]["count"] == 0

    def test_event_model_has_eval_fields(self) -> None:
        from callmem.models.events import Event

        event = Event(
            session_id="s", project_id="p", type="note", content="test",
        )
        assert event.eval_score is None
        assert event.eval_feedback is None
        assert event.eval_model is None
        row = event.to_row()
        assert "eval_score" in row
        assert "eval_feedback" in row
        assert "eval_model" in row

    def test_entity_model_has_eval_fields(self) -> None:
        from callmem.models.entities import Entity

        entity = Entity(
            project_id="p", type="fact", title="t", content="c",
        )
        assert entity.eval_score is None
        assert entity.eval_feedback is None
        row = entity.to_row()
        assert "eval_score" in row
        assert "eval_feedback" in row


class TestContextCompilation:
    """A4: Context Compilation API."""

    def test_compile_context_basic(self, engine: MemoryEngine) -> None:
        session = engine.start_session()
        engine.ingest_one("note", "Important decision about API design")
        engine.end_session(session.id)

        result = engine.compile_context(
            target_model="test-model",
            focus="API design",
            detail_level="standard",
        )
        assert "system_context" in result
        assert "tokens_estimated" in result
        assert "sources" in result
        assert isinstance(result["system_context"], str)
        assert result["tokens_estimated"] > 0

    def test_compile_context_with_tasks(self, engine: MemoryEngine) -> None:
        engine.create_task(title="Important task", task_type="coding")
        result = engine.compile_context(
            target_model="test-model",
            include_tasks=True,
        )
        assert "Important task" in result["system_context"]

    def test_compile_context_token_budget_truncation(self, engine: MemoryEngine) -> None:
        session = engine.start_session()
        for i in range(20):
            engine.ingest_one("note", f"Event {i} " * 100)
        engine.end_session(session.id)

        result = engine.compile_context(
            target_model="test-model",
            token_budget=50,
        )
        assert result["tokens_estimated"] <= 55
        assert "[... context truncated" in result["system_context"]

    def test_compile_context_detail_levels(self, engine: MemoryEngine) -> None:
        session = engine.start_session()
        engine.ingest_one("note", "Test content for detail levels")
        engine.end_session(session.id)

        for level in ("brief", "standard", "full"):
            result = engine.compile_context(
                target_model="test-model",
                focus="Test",
                detail_level=level,
            )
            assert "system_context" in result
            assert len(result["system_context"]) > 0

    def test_compile_context_with_model_registry(self, engine: MemoryEngine) -> None:
        engine.upsert_model({
            "model_name": "test-ctx-model",
            "context_window": 8000,
        })
        result = engine.compile_context(
            target_model="test-ctx-model",
            token_budget=None,
        )
        assert result["token_budget"] == 2000


class TestModelRegistry:
    """A5: Model Registry."""

    def test_upsert_and_get_model(self, engine: MemoryEngine) -> None:
        engine.upsert_model({
            "model_name": "anthropic/claude-sonnet-4",
            "provider": "anthropic",
            "display_name": "Claude Sonnet 4",
            "pricing_input": 3.0,
            "pricing_output": 15.0,
            "context_window": 200000,
            "supports_tools": True,
            "quality_tier": "strong",
        })
        info = engine.get_model_info("anthropic/claude-sonnet-4")
        assert info is not None
        assert info["provider"] == "anthropic"
        assert info["pricing_input"] == 3.0
        assert info["supports_tools"] is True

    def test_upsert_overwrites(self, engine: MemoryEngine) -> None:
        engine.upsert_model({
            "model_name": "test-model",
            "pricing_input": 5.0,
        })
        engine.upsert_model({
            "model_name": "test-model",
            "pricing_input": 2.0,
        })
        info = engine.get_model_info("test-model")
        assert info["pricing_input"] == 2.0

    def test_get_model_not_found(self, engine: MemoryEngine) -> None:
        assert engine.get_model_info("nonexistent") is None

    def test_list_models_by_provider(self, engine: MemoryEngine) -> None:
        engine.upsert_model({"model_name": "a/1", "provider": "anthropic"})
        engine.upsert_model({"model_name": "b/2", "provider": "openai"})
        engine.upsert_model({"model_name": "c/3", "provider": "anthropic"})

        anthropic = engine.list_models(provider="anthropic")
        assert len(anthropic) == 2

    def test_list_models_by_quality_tier(self, engine: MemoryEngine) -> None:
        engine.upsert_model({"model_name": "a/1", "quality_tier": "frontier"})
        engine.upsert_model({"model_name": "b/2", "quality_tier": "budget"})

        frontier = engine.list_models(quality_tier="frontier")
        assert len(frontier) == 1
        assert frontier[0]["model_name"] == "a/1"

    def test_list_models_max_price(self, engine: MemoryEngine) -> None:
        engine.upsert_model({"model_name": "a/1", "pricing_input": 1.0})
        engine.upsert_model({"model_name": "b/2", "pricing_input": 10.0})

        cheap = engine.list_models(max_price=5.0)
        assert len(cheap) == 1
        assert cheap[0]["model_name"] == "a/1"

    def test_list_models_require_tools(self, engine: MemoryEngine) -> None:
        engine.upsert_model({"model_name": "a/1", "supports_tools": True})
        engine.upsert_model({"model_name": "b/2", "supports_tools": False})

        with_tools = engine.list_models(require_tools=True)
        assert len(with_tools) == 1
        assert with_tools[0]["model_name"] == "a/1"

    def test_list_models_geo_filter(self, engine: MemoryEngine) -> None:
        engine.upsert_model({
            "model_name": "a/1",
            "geo_available": ["US", "EU"],
        })
        engine.upsert_model({
            "model_name": "b/2",
            "geo_available": ["EU"],
        })

        us_models = engine.list_models(geo_region="US")
        assert len(us_models) == 1
        assert us_models[0]["model_name"] == "a/1"

    def test_geo_check_available(self, engine: MemoryEngine) -> None:
        engine.upsert_model({
            "model_name": "test-geo",
            "geo_available": ["US", "EU"],
            "gateways": ["openrouter"],
        })
        result = engine.check_model_geo("test-geo", "US")
        assert result["available"] is True

    def test_geo_check_blocked(self, engine: MemoryEngine) -> None:
        engine.upsert_model({
            "model_name": "test-geo",
            "geo_blocked": ["CN"],
        })
        result = engine.check_model_geo("test-geo", "CN")
        assert result["available"] is False
        assert "CN" in result["reason"]

    def test_geo_check_not_in_available(self, engine: MemoryEngine) -> None:
        engine.upsert_model({
            "model_name": "test-geo",
            "geo_available": ["US"],
        })
        result = engine.check_model_geo("test-geo", "JP")
        assert result["available"] is False

    def test_geo_check_global(self, engine: MemoryEngine) -> None:
        engine.upsert_model({
            "model_name": "test-geo",
            "geo_available": ["*"],
        })
        result = engine.check_model_geo("test-geo", "XX")
        assert result["available"] is True

    def test_geo_check_model_not_found(self, engine: MemoryEngine) -> None:
        result = engine.check_model_geo("nonexistent", "US")
        assert result["available"] is None

    def test_recommend_model(self, engine: MemoryEngine) -> None:
        engine.upsert_model({
            "model_name": "a/strong",
            "quality_tier": "strong",
            "pricing_input": 3.0,
            "use_case_scores": {"coding": 0.9},
        })
        engine.upsert_model({
            "model_name": "b/budget",
            "quality_tier": "budget",
            "pricing_input": 0.5,
            "use_case_scores": {"coding": 0.5},
        })

        recs = engine.recommend_model("coding")
        assert len(recs) >= 2
        assert recs[0]["model_name"] == "a/strong"
        assert recs[0]["recommendation_score"] > recs[1]["recommendation_score"]

    def test_recommend_model_with_constraints(self, engine: MemoryEngine) -> None:
        engine.upsert_model({
            "model_name": "a/1",
            "context_window": 8000,
            "quality_tier": "standard",
        })
        engine.upsert_model({
            "model_name": "b/2",
            "context_window": 200000,
            "quality_tier": "frontier",
        })

        recs = engine.recommend_model("coding", min_context=100000)
        assert len(recs) == 1
        assert recs[0]["model_name"] == "b/2"

    def test_refresh_model(self, engine: MemoryEngine) -> None:
        engine.upsert_model({"model_name": "test-refresh"})
        result = engine.refresh_model("test-refresh")
        assert result["status"] == "refreshed"

    def test_refresh_all(self, engine: MemoryEngine) -> None:
        result = engine.refresh_model(None)
        assert result["status"] == "refresh_all"

    def test_model_registry_entry_serialization(self) -> None:
        entry = ModelRegistryEntry(
            model_name="test/model",
            provider="test",
            strengths=["coding", "reasoning"],
            benchmarks={"SWE-bench": 72.7},
            geo_available=["US", "EU"],
            use_case_scores={"coding": 0.9},
            supports_tools=True,
        )
        row = entry.to_row()
        assert row["strengths"] == json.dumps(["coding", "reasoning"])
        assert row["supports_tools"] == 1

        restored = ModelRegistryEntry.from_row(row)
        assert restored.strengths == ["coding", "reasoning"]
        assert restored.benchmarks == {"SWE-bench": 72.7}
        assert restored.supports_tools is True


class TestRewind:
    """A6: Undo / Rewind Support."""

    def test_create_rewind_point(self, engine: MemoryEngine) -> None:
        engine.start_session()
        engine.ingest_one("note", "Before rewind point")
        rp = engine.create_rewind_point(label="pre-test")
        assert rp["label"] == "pre-test"
        assert rp["event_count"] is not None
        assert rp["event_count"] >= 1

    def test_list_rewind_points(self, engine: MemoryEngine) -> None:
        engine.create_rewind_point(label="first")
        engine.create_rewind_point(label="second")
        points = engine.list_rewind_points()
        assert len(points) == 2
        assert points[0]["label"] == "second"

    def test_restore_rewind_point(self, engine: MemoryEngine) -> None:
        engine.start_session()
        engine.ingest_one("note", "Before rewind")
        rp = engine.create_rewind_point(label="checkpoint")

        time.sleep(0.01)
        engine.ingest_one("note", "After rewind")

        result = engine.restore_rewind_point(rp["id"])
        assert result["events_archived"] >= 1
        assert result["label"] == "checkpoint"

    def test_restore_nonexistent_raises(self, engine: MemoryEngine) -> None:
        with pytest.raises(ValueError, match="Rewind point not found"):
            engine.restore_rewind_point("nonexistent")

    def test_rewind_diff(self, engine: MemoryEngine) -> None:
        engine.start_session()
        engine.ingest_one("note", "Before checkpoint")
        rp = engine.create_rewind_point(label="checkpoint")

        time.sleep(0.01)
        engine.ingest_one("note", "After checkpoint")

        diff = engine.get_rewind_diff(rp["id"])
        assert diff["events_to_archive"] >= 1
        assert diff["label"] == "checkpoint"

    def test_rewind_diff_nonexistent_raises(self, engine: MemoryEngine) -> None:
        with pytest.raises(ValueError, match="Rewind point not found"):
            engine.get_rewind_diff("nonexistent")

    def test_restore_archives_entities(self, engine: MemoryEngine) -> None:
        from callmem.models.entities import Entity

        engine.start_session()
        engine.ingest_one("note", "Before checkpoint")
        rp = engine.create_rewind_point(label="checkpoint")

        time.sleep(0.01)
        entity = Entity(
            project_id=engine.project_id,
            type="fact",
            title="After checkpoint",
            content="Created after rewind point",
        )
        conn = engine.db.connect()
        try:
            row = entity.to_row()
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

        diff = engine.get_rewind_diff(rp["id"])
        assert diff["entities_to_archive"] >= 1

        result = engine.restore_rewind_point(rp["id"])
        assert result["entities_archived"] >= 1

    def test_restore_cancels_tasks(self, engine: MemoryEngine) -> None:
        rp = engine.create_rewind_point(label="before-tasks")

        time.sleep(0.01)
        engine.create_task(title="Task after checkpoint")

        result = engine.restore_rewind_point(rp["id"])
        assert result["tasks_cancelled"] >= 1


class TestMCPToolHandlers:
    """Verify the MCP tool handler functions work correctly."""

    def test_all_new_tools_registered(self) -> None:
        from callmem.mcp.tools import _HANDLERS

        expected = [
            "mem_task_create", "mem_task_update", "mem_task_list",
            "mem_task_tree",
            "mem_model_stats", "mem_model_compare",
            "mem_eval", "mem_eval_summary",
            "mem_compile_context",
            "mem_model_list", "mem_model_info", "mem_model_recommend",
            "mem_model_geo_check", "mem_model_refresh",
            "mem_rewind_create", "mem_rewind_list",
            "mem_rewind_restore", "mem_rewind_diff",
        ]
        for name in expected:
            assert name in _HANDLERS, f"{name} not registered in _HANDLERS"

    def test_all_new_tools_defined(self) -> None:
        from callmem.mcp.tools import TOOL_DEFINITIONS

        names = {t["name"] for t in TOOL_DEFINITIONS}
        expected = [
            "mem_task_create", "mem_task_update", "mem_task_list",
            "mem_task_tree",
            "mem_model_stats", "mem_model_compare",
            "mem_eval", "mem_eval_summary",
            "mem_compile_context",
            "mem_model_list", "mem_model_info", "mem_model_recommend",
            "mem_model_geo_check", "mem_model_refresh",
            "mem_rewind_create", "mem_rewind_list",
            "mem_rewind_restore", "mem_rewind_diff",
        ]
        for name in expected:
            assert name in names, f"{name} not in TOOL_DEFINITIONS"

    def test_task_create_handler(self, engine: MemoryEngine) -> None:
        from callmem.mcp.tools import handle_task_create

        result = handle_task_create(engine, {"title": "Handler test"})
        import json
        data = json.loads(result[0].text)
        assert data["title"] == "Handler test"
        assert data["status"] == "pending"

    def test_eval_handler_auto_detect_event(self, engine: MemoryEngine) -> None:
        from callmem.mcp.tools import handle_eval

        engine.start_session()
        engine.ingest_one("note", "Test event")
        events = engine.get_events(limit=1)

        result = handle_eval(engine, {"id": events[0].id, "score": 0.9})
        import json
        data = json.loads(result[0].text)
        assert data["eval_score"] == 0.9

    def test_rewind_create_handler(self, engine: MemoryEngine) -> None:
        from callmem.mcp.tools import handle_rewind_create

        result = handle_rewind_create(engine, {"label": "test"})
        import json
        data = json.loads(result[0].text)
        assert data["label"] == "test"
        assert "id" in data


class TestMigrations:
    """Verify new migrations applied correctly."""

    def test_schema_version_14(self, memory_db: Database) -> None:
        assert memory_db.get_schema_version() == 14

    def test_tasks_table_exists(self, memory_db: Database) -> None:
        tables = memory_db.list_tables()
        assert "tasks" in tables

    def test_model_stats_table_exists(self, memory_db: Database) -> None:
        tables = memory_db.list_tables()
        assert "model_stats" in tables

    def test_model_registry_table_exists(self, memory_db: Database) -> None:
        tables = memory_db.list_tables()
        assert "model_registry" in tables

    def test_rewind_points_table_exists(self, memory_db: Database) -> None:
        tables = memory_db.list_tables()
        assert "rewind_points" in tables

    def test_events_has_eval_columns(self, memory_db: Database) -> None:
        conn = memory_db.connect()
        try:
            cols = {c["name"] for c in conn.execute("PRAGMA table_info(events)").fetchall()}
            assert "eval_score" in cols
            assert "eval_feedback" in cols
            assert "eval_model" in cols
        finally:
            conn.close()

    def test_entities_has_eval_columns(self, memory_db: Database) -> None:
        conn = memory_db.connect()
        try:
            cols = {c["name"] for c in conn.execute("PRAGMA table_info(entities)").fetchall()}
            assert "eval_score" in cols
            assert "eval_feedback" in cols
        finally:
            conn.close()
