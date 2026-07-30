"""Tests for entity extraction from events."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import patch

from callmem.core.extraction import EntityExtractor
from callmem.core.ollama import OllamaClient
from callmem.core.queue import JobQueue
from callmem.models.config import Config

if TYPE_CHECKING:
    from callmem.core.database import Database
    pass


def _setup_engine_and_extractor(
    memory_db: Database,
) -> tuple:
    from callmem.core.engine import MemoryEngine

    config = Config(sensitive_data={"enabled": False, "llm_scan": False})
    engine = MemoryEngine(memory_db, config)
    extractor = EntityExtractor(memory_db, OllamaClient())
    return engine, extractor


def _assert_pending_job(db: Database, job_type: str) -> None:
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT id FROM jobs WHERE type = ? AND status = 'pending' LIMIT 1",
            (job_type,),
        ).fetchone()
        assert row is not None, f"No pending {job_type} job found"
    finally:
        conn.close()


class TestEntityExtractor:
    def test_extracts_decisions(self, memory_db: Database) -> None:
        engine, extractor = _setup_engine_and_extractor(memory_db)
        engine.start_session()
        event = engine.ingest_one(
            "response",
            "I recommend using Redis for caching because it is fast",
        )
        assert event is not None

        llm_response = (
            '{"decisions": [{"title": "Use Redis", "content": "Chose Redis for caching", '
            '"key_points": ["Redis chosen for caching", "Fast in-memory store"], '
            '"synopsis": "Decided to use Redis for the caching layer due to its speed."}],'
            '"todos": [], "facts": [], "failures": [], "discoveries": [], '
            '"features": [], "bugfixes": [], "research": [], "changes": []}'
        )
        with patch.object(extractor.ollama, "_generate", return_value=llm_response):
            entities = extractor.process_pending()

        assert len(entities) == 1
        assert entities[0].type == "decision"
        assert entities[0].title == "Use Redis"
        assert entities[0].key_points is not None
        assert "Redis chosen for caching" in entities[0].key_points
        assert entities[0].synopsis is not None
        assert "Redis" in entities[0].synopsis

    def test_extracts_todos(self, memory_db: Database) -> None:
        engine, extractor = _setup_engine_and_extractor(memory_db)
        engine.start_session()
        event = engine.ingest_one("response", "We need to add auth middleware")
        assert event is not None

        llm_response = (
            '{"decisions": [], "todos": ['
            '{"title": "Add auth middleware", "content": "Implement auth", '
            '"priority": "high", "status": "open"}],'
            '"facts": [], "failures": [], "discoveries": [], '
            '"features": [], "bugfixes": [], "research": [], "changes": []}'
        )
        with patch.object(extractor.ollama, "_generate", return_value=llm_response):
            entities = extractor.process_pending()

        assert len(entities) == 1
        assert entities[0].type == "todo"
        assert entities[0].priority == "high"

    def test_extracts_multiple_categories(
        self, memory_db: Database
    ) -> None:
        engine, extractor = _setup_engine_and_extractor(memory_db)
        engine.start_session()
        event = engine.ingest_one("response", "Decided to use FastAPI. Got a 500 error.")
        assert event is not None

        llm_response = (
            '{"decisions": [{"title": "Use FastAPI", "content": "Chose FastAPI"}],'
            '"todos": [],'
            '"facts": [],'
            '"failures": [{"title": "500 error", '
            '"content": "Got a server error", "status": "unresolved"}],'
            '"discoveries": [], '
            '"features": [], "bugfixes": [], "research": [], "changes": []}'
        )
        with patch.object(extractor.ollama, "_generate", return_value=llm_response):
            entities = extractor.process_pending()

        assert len(entities) == 2
        types = {e.type for e in entities}
        assert "decision" in types
        assert "failure" in types

    def test_entity_linked_to_source_event(
        self, memory_db: Database
    ) -> None:
        engine, extractor = _setup_engine_and_extractor(memory_db)
        engine.start_session()
        event = engine.ingest_one("response", "Use SQLite for storage")
        assert event is not None

        llm_response = (
            '{"decisions": [{"title": "Use SQLite", "content": "Storage"}],'
            '"todos": [], "facts": [], "failures": [], "discoveries": [], '
            '"features": [], "bugfixes": [], "research": [], "changes": []}'
        )
        with patch.object(extractor.ollama, "_generate", return_value=llm_response):
            entities = extractor.process_pending()

        assert len(entities) == 1
        assert entities[0].source_event_id == event.id

    def test_entities_carry_full_source_event_ids(
        self, memory_db: Database
    ) -> None:
        """A batch job spanning multiple events must record every event id
        on each extracted entity, not just the first (phase0-reliability
        task 6)."""
        from callmem.models.events import EventInput

        engine, extractor = _setup_engine_and_extractor(memory_db)
        engine.start_session()
        events = engine.ingest([
            EventInput(type="note", content="event 1"),
            EventInput(type="note", content="event 2"),
        ])
        assert len(events) == 2
        event_ids = [e.id for e in events]

        llm_response = (
            '{"decisions": [{"title": "Use Redis", "content": "Caching"}],'
            '"todos": [], "facts": [], "failures": [], "discoveries": [], '
            '"features": [], "bugfixes": [], "research": [], "changes": []}'
        )
        with patch.object(extractor.ollama, "_generate", return_value=llm_response):
            entities = extractor.process_pending()

        assert len(entities) == 1
        assert entities[0].source_event_id == event_ids[0]
        assert entities[0].source_event_ids == event_ids

        conn = memory_db.connect()
        try:
            row = conn.execute(
                "SELECT source_event_ids FROM entities WHERE id = ?",
                (entities[0].id,),
            ).fetchone()
            assert json.loads(row["source_event_ids"]) == event_ids
        finally:
            conn.close()

    def test_invalid_json_returns_empty(self, memory_db: Database) -> None:
        engine, extractor = _setup_engine_and_extractor(memory_db)
        engine.start_session()
        engine.ingest_one("response", "some text")

        with patch.object(
            extractor.ollama, "_generate", return_value="not valid json"
        ):
            entities = extractor.process_pending()

        assert entities == []

    def test_ollama_failure_defers_retry_with_backoff(
        self, memory_db: Database
    ) -> None:
        """A failure schedules a backoff retry rather than burning through
        all attempts in the same drain pass (phase0-reliability task 2)."""
        engine, extractor = _setup_engine_and_extractor(memory_db)
        engine.start_session()
        engine.ingest_one("response", "some content")

        with patch.object(
            extractor.ollama, "_generate", return_value=None
        ):
            entities = extractor.process_pending()

        assert entities == []

        queue = JobQueue(memory_db)
        # Still pending — deferred for retry, not burned through instantly.
        assert queue.get_pending_count("extract_entities") == 1
        assert queue.dequeue("extract_entities") is None  # backoff not yet elapsed

        conn = memory_db.connect()
        try:
            row = conn.execute(
                "SELECT * FROM jobs WHERE type = 'extract_entities' LIMIT 1"
            ).fetchone()
            assert row is not None
            assert row["status"] == "pending"
            assert row["next_attempt_at"] is not None
            assert "Ollama returned no response" in row["error"]
        finally:
            conn.close()

    def test_no_pending_jobs_returns_empty(
        self, memory_db: Database
    ) -> None:
        _, extractor = _setup_engine_and_extractor(memory_db)
        entities = extractor.process_pending()
        assert entities == []

    def test_extracts_new_entity_types(
        self, memory_db: Database
    ) -> None:
        engine, extractor = _setup_engine_and_extractor(memory_db)
        engine.start_session()
        event = engine.ingest_one("response", "Added export feature, fixed a bug")
        assert event is not None

        llm_response = (
            '{"decisions": [], "todos": [], "facts": [], "failures": [], '
            '"discoveries": [], '
            '"features": [{"title": "Export feature", "content": "Added export"}], '
            '"bugfixes": [{"title": "Fixed bug", "content": "Fixed null pointer"}], '
            '"research": [], "changes": []}'
        )
        with patch.object(extractor.ollama, "_generate", return_value=llm_response):
            entities = extractor.process_pending()

        assert len(entities) == 2
        types = {e.type for e in entities}
        assert "feature" in types
        assert "bugfix" in types

    def test_key_points_fallback_to_content(
        self, memory_db: Database
    ) -> None:
        engine, extractor = _setup_engine_and_extractor(memory_db)
        engine.start_session()
        engine.ingest_one("response", "some content")

        llm_response = (
            '{"decisions": [{"title": "Test", "content": "The content"}],'
            '"todos": [], "facts": [], "failures": [], "discoveries": [], '
            '"features": [], "bugfixes": [], "research": [], "changes": []}'
        )
        with patch.object(extractor.ollama, "_generate", return_value=llm_response):
            entities = extractor.process_pending()

        assert len(entities) == 1
        assert entities[0].key_points is None
        assert entities[0].synopsis is None


class TestFileAnchorExtraction:
    """Deterministic file:line anchors parsed from entity content,
    independent of whatever the LLM put in the "files" field."""

    def test_parses_path_and_line_from_content_into_entity_files(
        self, memory_db: Database,
    ) -> None:
        engine, extractor = _setup_engine_and_extractor(memory_db)
        engine.start_session()
        event = engine.ingest_one(
            "response", "Fixed the FK constraint bug",
        )
        assert event is not None

        llm_response = (
            '{"decisions": [], "todos": [], "facts": [], "failures": [], '
            '"discoveries": [], "features": [], '
            '"bugfixes": [{"title": "Fix FK constraint", '
            '"content": "Fixed FK constraint bug in '
            'src/callmem/core/repository.py:842"}], '
            '"research": [], "changes": []}'
        )
        with patch.object(extractor.ollama, "_generate", return_value=llm_response):
            entities = extractor.process_pending()

        assert len(entities) == 1
        from callmem.core.repository import Repository

        repo = Repository(memory_db)
        files = repo.get_files_for_entity(entities[0].id)
        assert {
            "file_path": "src/callmem/core/repository.py",
            "relation": "related",
            "line_number": 842,
        } in files

    def test_content_without_file_references_inserts_nothing(
        self, memory_db: Database,
    ) -> None:
        engine, extractor = _setup_engine_and_extractor(memory_db)
        engine.start_session()
        event = engine.ingest_one("response", "Discussed roadmap priorities")
        assert event is not None

        llm_response = (
            '{"decisions": [{"title": "Prioritize v2", '
            '"content": "No files involved, just a decision"}],'
            '"todos": [], "facts": [], "failures": [], "discoveries": [], '
            '"features": [], "bugfixes": [], "research": [], "changes": []}'
        )
        with patch.object(extractor.ollama, "_generate", return_value=llm_response):
            entities = extractor.process_pending()

        assert len(entities) == 1
        from callmem.core.repository import Repository

        repo = Repository(memory_db)
        assert repo.get_files_for_entity(entities[0].id) == []

    def test_llm_provided_files_still_inserted_alongside_parsed_anchors(
        self, memory_db: Database,
    ) -> None:
        """The existing LLM "files" list keeps working — content-parsed
        anchors extend it, they don't replace it."""
        engine, extractor = _setup_engine_and_extractor(memory_db)
        engine.start_session()
        event = engine.ingest_one("response", "Added a new feature")
        assert event is not None

        llm_response = (
            '{"decisions": [], "todos": [], "facts": [], "failures": [], '
            '"discoveries": [], '
            '"features": [{"title": "Add widget", '
            '"content": "Added a widget, no inline path here", '
            '"files": ["src/callmem/widget.py"]}], '
            '"bugfixes": [], "research": [], "changes": []}'
        )
        with patch.object(extractor.ollama, "_generate", return_value=llm_response):
            entities = extractor.process_pending()

        assert len(entities) == 1
        from callmem.core.repository import Repository

        repo = Repository(memory_db)
        files = repo.get_files_for_entity(entities[0].id)
        assert {
            "file_path": "src/callmem/widget.py",
            "relation": "related",
            "line_number": None,
        } in files


class TestFormatEvents:
    def test_format_events_includes_tool_result_content(
        self, memory_db: Database,
    ) -> None:
        _, extractor = _setup_engine_and_extractor(memory_db)
        formatted = extractor._format_events([
            {"type": "tool_call", "content": "Bash(ls)"},
            {"type": "tool_result", "content": "file1.txt\nfile2.txt"},
        ])
        assert "file1.txt" in formatted
        assert "[tool_result]" in formatted

    def test_tool_result_event_reaches_extraction_prompt(
        self, memory_db: Database,
    ) -> None:
        engine, extractor = _setup_engine_and_extractor(memory_db)
        engine.start_session()
        event = engine.ingest_one(
            "tool_result", "traceback: KeyError: 'missing_field'",
        )
        assert event is not None

        captured_prompts: list[str] = []

        def _fake_extract(prompt: str) -> str:
            captured_prompts.append(prompt)
            return (
                '{"decisions": [], "todos": [], "facts": [], "failures": [], '
                '"discoveries": [], "features": [], "bugfixes": [], '
                '"research": [], "changes": []}'
            )

        with patch.object(extractor.ollama, "extract", side_effect=_fake_extract):
            extractor.process_pending()

        assert len(captured_prompts) == 1
        assert "KeyError: 'missing_field'" in captured_prompts[0]


class TestEventBusHandling:
    def test_no_error_when_event_bus_is_none(self, memory_db: Database) -> None:
        engine, extractor = _setup_engine_and_extractor(memory_db)
        assert extractor.event_bus is None
        engine.start_session()
        event = engine.ingest_one("response", "We chose PostgreSQL for the DB")
        assert event is not None

        llm_response = (
            '{"decisions": [{"title": "Use PostgreSQL", "content": "DB choice"}],'
            '"todos": [], "facts": [], "failures": [], "discoveries": [], '
            '"features": [], "bugfixes": [], "research": [], "changes": []}'
        )
        with patch.object(extractor.ollama, "_generate", return_value=llm_response):
            entities = extractor.process_pending()

        assert len(entities) == 1
        assert entities[0].type == "decision"

    def test_publish_called_when_event_bus_provided(self, memory_db: Database) -> None:
        from unittest.mock import MagicMock

        event_bus = MagicMock()
        engine, extractor = _setup_engine_and_extractor(memory_db)
        extractor.event_bus = event_bus
        engine.start_session()
        event = engine.ingest_one("response", "We decided on SQLite")
        assert event is not None

        llm_response = (
            '{"decisions": [{"title": "Use SQLite", "content": "Storage choice"}],'
            '"todos": [], "facts": [], "failures": [], "discoveries": [], '
            '"features": [], "bugfixes": [], "research": [], "changes": []}'
        )
        with patch.object(extractor.ollama, "_generate", return_value=llm_response):
            entities = extractor.process_pending()

        assert len(entities) == 1
        event_bus.publish.assert_called_once()
        call_args = event_bus.publish.call_args
        assert call_args[0][0] == "entity_created"
        assert call_args[0][1]["title"] == "Use SQLite"


class TestIngestQueuesExtraction:
    def test_ingest_creates_extraction_job(
        self, memory_db: Database
    ) -> None:
        engine, _ = _setup_engine_and_extractor(memory_db)
        engine.start_session()
        engine.ingest_one("note", "some event content")

        queue = JobQueue(memory_db)
        assert queue.get_pending_count("extract_entities") == 1

    def test_ingest_multiple_events_single_job(
        self, memory_db: Database
    ) -> None:
        engine, _ = _setup_engine_and_extractor(memory_db)
        engine.start_session()
        from callmem.models.events import EventInput

        engine.ingest([
            EventInput(type="note", content="event 1"),
            EventInput(type="note", content="event 2"),
        ])

        queue = JobQueue(memory_db)
        assert queue.get_pending_count("extract_entities") == 1

        job = queue.dequeue("extract_entities")
        assert job is not None
        assert len(job.payload["event_ids"]) == 2


class TestAutoResolution:
    def test_bugfix_resolves_matching_todo(self, memory_db: Database) -> None:
        engine, extractor = _setup_engine_and_extractor(memory_db)
        engine.start_session()

        from callmem.models.entities import Entity

        todo = Entity(
            project_id=engine.project_id,
            type="todo",
            title="Fix copy button clipboard fallback",
            content="The copy button fails on non-HTTPS",
            status="open",
            priority="high",
        )
        conn = memory_db.connect()
        row = todo.to_row()
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

        engine.ingest_one("note", "Fixed the copy button clipboard fallback")

        _assert_pending_job(memory_db, "extract_entities")

        llm_response = (
            '{"decisions": [], "todos": [], "facts": [], "failures": [], '
            '"discoveries": [], "features": [], '
            '"bugfixes": [{"title": "Fixed copy button clipboard fallback", '
            '"content": "Added fallback copy mechanism", '
            '"key_points": ["navigator.clipboard fails on non-HTTPS"], '
            '"synopsis": "Fixed by adding execCommand fallback"}], '
            '"research": [], "changes": []}'
        )

        with patch.object(extractor.ollama, "_generate", return_value=llm_response):
            entities = extractor.process_pending()

        assert len(entities) == 1
        assert entities[0].type == "bugfix"

        updated = conn.execute(
            "SELECT status, resolved_at, stale FROM entities WHERE id = ?",
            (todo.id,),
        ).fetchone()
        conn.close()
        assert updated["status"] == "done"
        # Auto-resolve now shares Repository.mark_resolved with mem_resolve,
        # so every close-out path -- automatic or manual -- stamps
        # resolved_at and clears stale identically.
        assert updated["resolved_at"] is not None
        assert updated["stale"] == 0

    def test_feature_resolves_matching_todo(self, memory_db: Database) -> None:
        engine, extractor = _setup_engine_and_extractor(memory_db)
        engine.start_session()

        from callmem.models.entities import Entity

        todo = Entity(
            project_id=engine.project_id,
            type="todo",
            title="Implement analysis history selector",
            content="Need a dropdown to pick past analysis runs",
            status="open",
            priority="medium",
        )
        conn = memory_db.connect()
        row = todo.to_row()
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

        engine.ingest_one("note", "Built the analysis history selector")

        _assert_pending_job(memory_db, "extract_entities")

        llm_response = (
            '{"decisions": [], "todos": [], "facts": [], "failures": [], '
            '"discoveries": [], '
            '"features": [{"title": "Analysis history selector implemented", '
            '"content": "Dropdown to pick past runs", '
            '"key_points": ["Uses analysis_results table"], '
            '"synopsis": "Implemented UI component"}], '
            '"bugfixes": [], "research": [], "changes": []}'
        )

        with patch.object(extractor.ollama, "_generate", return_value=llm_response):
            extractor.process_pending()

        updated = conn.execute(
            "SELECT status FROM entities WHERE id = ?", (todo.id,)
        ).fetchone()
        conn.close()
        assert updated["status"] == "done"

    def test_no_resolve_when_unrelated(self, memory_db: Database) -> None:
        engine, extractor = _setup_engine_and_extractor(memory_db)
        engine.start_session()

        from callmem.models.entities import Entity

        todo = Entity(
            project_id=engine.project_id,
            type="todo",
            title="Configure Redis caching layer",
            content="Set up Redis for session storage",
            status="open",
            priority="medium",
        )
        conn = memory_db.connect()
        row = todo.to_row()
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

        engine.ingest_one("note", "Fixed the copy button clipboard fallback")
        _assert_pending_job(memory_db, "extract_entities")

        llm_response = (
            '{"decisions": [], "todos": [], "facts": [], "failures": [], '
            '"discoveries": [], "features": [], '
            '"bugfixes": [{"title": "Fixed copy button clipboard fallback", '
            '"content": "Added fallback copy mechanism", '
            '"key_points": ["navigator.clipboard fails"], '
            '"synopsis": "Fixed"}], '
            '"research": [], "changes": []}'
        )

        with patch.object(extractor.ollama, "_generate", return_value=llm_response):
            extractor.process_pending()

        updated = conn.execute(
            "SELECT status FROM entities WHERE id = ?", (todo.id,)
        ).fetchone()
        conn.close()
        assert updated["status"] == "open"


class TestSweepResolutions:
    """Retroactive sweep closes items the live hook missed."""

    @staticmethod
    def _insert_entity(
        db: Database,
        project_id: str,
        type: str,
        title: str,
        status: str | None = None,
        stale: int = 0,
    ) -> str:
        from callmem.models.entities import Entity

        entity = Entity(
            project_id=project_id, type=type, title=title,
            content=title, status=status,
        )
        row = entity.to_row()
        conn = db.connect()
        try:
            conn.execute(
                "INSERT INTO entities "
                "(id, project_id, source_event_id, type, title, content, "
                "key_points, synopsis, status, priority, pinned, "
                "created_at, updated_at, resolved_at, metadata, "
                "archived_at, stale) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    row["id"], row["project_id"], row["source_event_id"],
                    row["type"], row["title"], row["content"],
                    row["key_points"], row["synopsis"], row["status"],
                    row["priority"], row["pinned"], row["created_at"],
                    row["updated_at"], row["resolved_at"], row["metadata"],
                    row["archived_at"], stale,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return entity.id

    def test_sweep_closes_todo_missed_by_live_hook(
        self, memory_db: Database,
    ) -> None:
        """Driver inserted before the TODO — live hook misses, sweep catches."""
        engine, extractor = _setup_engine_and_extractor(memory_db)
        # Feature inserted first (as if extracted before TODO existed)
        self._insert_entity(
            memory_db, engine.project_id, "feature",
            "Analysis history selector implemented",
        )
        # TODO inserted afterwards — never matched by live auto-resolve
        todo_id = self._insert_entity(
            memory_db, engine.project_id, "todo",
            "Implement analysis history selector",
            status="open",
        )

        records = extractor.sweep_resolutions(engine.project_id)
        assert any(r["id"] == todo_id for r in records)

        conn = memory_db.connect()
        row = conn.execute(
            "SELECT status FROM entities WHERE id = ?", (todo_id,),
        ).fetchone()
        conn.close()
        assert row["status"] == "done"

    def test_sweep_dry_run_does_not_modify(
        self, memory_db: Database,
    ) -> None:
        engine, extractor = _setup_engine_and_extractor(memory_db)
        self._insert_entity(
            memory_db, engine.project_id, "feature",
            "Analysis history selector implemented",
        )
        todo_id = self._insert_entity(
            memory_db, engine.project_id, "todo",
            "Implement analysis history selector",
            status="open",
        )

        records = extractor.sweep_resolutions(
            engine.project_id, dry_run=True,
        )
        assert any(r["id"] == todo_id for r in records)

        conn = memory_db.connect()
        row = conn.execute(
            "SELECT status FROM entities WHERE id = ?", (todo_id,),
        ).fetchone()
        conn.close()
        assert row["status"] == "open"

    def test_sweep_ignores_stale_drivers(
        self, memory_db: Database,
    ) -> None:
        engine, extractor = _setup_engine_and_extractor(memory_db)
        self._insert_entity(
            memory_db, engine.project_id, "feature",
            "Analysis history selector implemented",
            stale=1,
        )
        todo_id = self._insert_entity(
            memory_db, engine.project_id, "todo",
            "Implement analysis history selector",
            status="open",
        )

        records = extractor.sweep_resolutions(engine.project_id)
        assert not any(r["id"] == todo_id for r in records)

        conn = memory_db.connect()
        row = conn.execute(
            "SELECT status FROM entities WHERE id = ?", (todo_id,),
        ).fetchone()
        conn.close()
        assert row["status"] == "open"

    def test_sweep_skips_already_closed(
        self, memory_db: Database,
    ) -> None:
        engine, extractor = _setup_engine_and_extractor(memory_db)
        self._insert_entity(
            memory_db, engine.project_id, "feature",
            "Analysis history selector implemented",
        )
        # Already resolved — should not show up as "closed" in the sweep
        todo_id = self._insert_entity(
            memory_db, engine.project_id, "todo",
            "Implement analysis history selector",
            status="done",
        )

        records = extractor.sweep_resolutions(engine.project_id)
        assert not any(r["id"] == todo_id for r in records)

    def test_sweep_with_no_drivers_returns_empty(
        self, memory_db: Database,
    ) -> None:
        engine, extractor = _setup_engine_and_extractor(memory_db)
        self._insert_entity(
            memory_db, engine.project_id, "todo",
            "Something lonely", status="open",
        )
        records = extractor.sweep_resolutions(engine.project_id)
        assert records == []


class TestAutoResolveDiscussionGuard:
    """Discussing a TODO must not be mistaken for completing it.

    Reproduces the triage incident: an agent discussed 69 open todo/
    failure entities by short ID. Extraction turned that discussion
    into feature/change "driver" entities, and the auto-resolve sweep
    keyword-matched those drivers against the very entities under
    discussion -- closing several of them mid-triage. The guard: if a
    driver's own source text quotes the target's short (or full) ID,
    treat that as discussion, not completion, and skip the resolution.
    """

    @staticmethod
    def _insert_entity(
        db: Database,
        project_id: str,
        type: str,
        title: str,
        status: str | None = None,
        stale: int = 0,
        source_event_ids: list[str] | None = None,
    ) -> str:
        from callmem.models.entities import Entity

        entity = Entity(
            project_id=project_id, type=type, title=title, content=title,
            status=status, source_event_ids=source_event_ids,
        )
        row = entity.to_row()
        conn = db.connect()
        try:
            conn.execute(
                "INSERT INTO entities "
                "(id, project_id, source_event_id, source_event_ids, type, "
                "title, content, key_points, synopsis, status, priority, "
                "pinned, created_at, updated_at, resolved_at, metadata, "
                "archived_at, stale) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    row["id"], row["project_id"], row["source_event_id"],
                    row["source_event_ids"], row["type"], row["title"],
                    row["content"], row["key_points"], row["synopsis"],
                    row["status"], row["priority"], row["pinned"],
                    row["created_at"], row["updated_at"], row["resolved_at"],
                    row["metadata"], row["archived_at"], stale,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return entity.id

    def test_guard_blocks_resolution_when_driver_discusses_target_id(
        self, memory_db: Database,
    ) -> None:
        """Driver's source event quotes the target's '#'+short-id -- a
        triage discussion, not genuine completion work. Must survive."""
        engine, extractor = _setup_engine_and_extractor(memory_db)
        engine.start_session()

        todo_id = self._insert_entity(
            memory_db, engine.project_id, "todo",
            "Implement analysis history selector", status="open",
        )
        short_id = todo_id[-8:]

        discussion_event = engine.ingest_one(
            "response",
            f"Reviewing open items in triage: #{short_id} analysis "
            "history selector is still outstanding, discussed with team.",
        )
        assert discussion_event is not None

        self._insert_entity(
            memory_db, engine.project_id, "feature",
            "Analysis history selector implemented",
            source_event_ids=[discussion_event.id],
        )

        records = extractor.sweep_resolutions(engine.project_id)
        assert not any(r["id"] == todo_id for r in records)

        conn = memory_db.connect()
        row = conn.execute(
            "SELECT status FROM entities WHERE id = ?", (todo_id,),
        ).fetchone()
        conn.close()
        assert row["status"] == "open"

    def test_guard_allows_genuine_driver_without_id_mention(
        self, memory_db: Database,
    ) -> None:
        """Driver's source event never quotes the target's ID -- genuine
        completion work. Must still close as before."""
        engine, extractor = _setup_engine_and_extractor(memory_db)
        engine.start_session()

        todo_id = self._insert_entity(
            memory_db, engine.project_id, "todo",
            "Implement analysis history selector", status="open",
        )

        completion_event = engine.ingest_one(
            "response",
            "Finished wiring up the analysis history selector component.",
        )
        assert completion_event is not None

        self._insert_entity(
            memory_db, engine.project_id, "feature",
            "Analysis history selector implemented",
            source_event_ids=[completion_event.id],
        )

        records = extractor.sweep_resolutions(engine.project_id)
        assert any(r["id"] == todo_id for r in records)

        conn = memory_db.connect()
        row = conn.execute(
            "SELECT status FROM entities WHERE id = ?", (todo_id,),
        ).fetchone()
        conn.close()
        assert row["status"] == "done"

    def test_guard_skip_count_surfaces_in_stats(
        self, memory_db: Database,
    ) -> None:
        """The skipped-by-guard count is observable via the ``stats``
        dict, which the CLI's --dry-run output reports from."""
        engine, extractor = _setup_engine_and_extractor(memory_db)
        engine.start_session()

        todo_id = self._insert_entity(
            memory_db, engine.project_id, "todo",
            "Implement analysis history selector", status="open",
        )
        short_id = todo_id[-8:]
        discussion_event = engine.ingest_one(
            "response",
            f"Triage note: #{short_id} still open, discussed with team.",
        )
        assert discussion_event is not None
        self._insert_entity(
            memory_db, engine.project_id, "feature",
            "Analysis history selector implemented",
            source_event_ids=[discussion_event.id],
        )

        stats: dict[str, int] = {}
        records = extractor.sweep_resolutions(
            engine.project_id, dry_run=True, stats=stats,
        )
        assert records == []
        assert stats.get("skipped_by_guard") == 1

    def test_guard_applies_to_live_auto_resolve_path(
        self, memory_db: Database,
    ) -> None:
        """The guard covers the in-session sweep (``_auto_resolve``,
        run right after extraction), not just the CLI retroactive sweep
        -- both share ``_resolve_by_drivers``, but this confirms it."""
        engine, extractor = _setup_engine_and_extractor(memory_db)
        engine.start_session()

        todo_id = self._insert_entity(
            memory_db, engine.project_id, "todo",
            "Implement analysis history selector", status="open",
        )
        short_id = todo_id[-8:]

        event = engine.ingest_one(
            "response",
            f"Triage: #{short_id} analysis history selector still pending.",
        )
        assert event is not None

        llm_response = (
            '{"decisions": [], "todos": [], "facts": [], "failures": [], '
            '"discoveries": [], "features": [], "bugfixes": [], '
            '"research": [], "changes": [{"title": '
            '"Analysis history selector implemented", '
            '"content": "Closed out the selector work."}]}'
        )
        with patch.object(extractor.ollama, "_generate", return_value=llm_response):
            extractor.process_pending()

        conn = memory_db.connect()
        row = conn.execute(
            "SELECT status FROM entities WHERE id = ?", (todo_id,),
        ).fetchone()
        conn.close()
        assert row["status"] == "open"


class TestGatherResolutionCandidates:
    """Recall stage for the judged sweep (``callmem resolve``'s default
    mode): matches become candidates for ``ResolutionJudge`` to verify --
    unlike ``sweep_resolutions`` (the legacy/--no-judge path), nothing is
    closed here."""

    @staticmethod
    def _insert_entity(
        db: Database,
        project_id: str,
        type: str,
        title: str,
        status: str | None = None,
        stale: int = 0,
        source_event_ids: list[str] | None = None,
    ) -> str:
        from callmem.models.entities import Entity

        entity = Entity(
            project_id=project_id, type=type, title=title, content=title,
            status=status, source_event_ids=source_event_ids,
        )
        row = entity.to_row()
        conn = db.connect()
        try:
            conn.execute(
                "INSERT INTO entities "
                "(id, project_id, source_event_id, source_event_ids, type, "
                "title, content, key_points, synopsis, status, priority, "
                "pinned, created_at, updated_at, resolved_at, metadata, "
                "archived_at, stale) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    row["id"], row["project_id"], row["source_event_id"],
                    row["source_event_ids"], row["type"], row["title"],
                    row["content"], row["key_points"], row["synopsis"],
                    row["status"], row["priority"], row["pinned"],
                    row["created_at"], row["updated_at"], row["resolved_at"],
                    row["metadata"], row["archived_at"], stale,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return entity.id

    def test_keyword_match_becomes_candidate_not_closed(
        self, memory_db: Database,
    ) -> None:
        engine, extractor = _setup_engine_and_extractor(memory_db)
        self._insert_entity(
            memory_db, engine.project_id, "feature",
            "Analysis history selector implemented",
        )
        todo_id = self._insert_entity(
            memory_db, engine.project_id, "todo",
            "Implement analysis history selector", status="open",
        )

        candidates = extractor.gather_resolution_candidates(engine.project_id)
        assert any(c.target_id == todo_id for c in candidates)

        conn = memory_db.connect()
        row = conn.execute(
            "SELECT status FROM entities WHERE id = ?", (todo_id,),
        ).fetchone()
        conn.close()
        # Unlike sweep_resolutions, gathering candidates never closes.
        assert row["status"] == "open"

    def test_candidate_carries_driver_and_target_content(
        self, memory_db: Database,
    ) -> None:
        engine, extractor = _setup_engine_and_extractor(memory_db)
        driver_id = self._insert_entity(
            memory_db, engine.project_id, "feature",
            "Analysis history selector implemented",
        )
        todo_id = self._insert_entity(
            memory_db, engine.project_id, "todo",
            "Implement analysis history selector", status="open",
        )

        candidates = extractor.gather_resolution_candidates(engine.project_id)
        match = next(c for c in candidates if c.target_id == todo_id)
        assert match.driver_id == driver_id
        assert match.driver_title == "Analysis history selector implemented"
        assert match.driver_content == "Analysis history selector implemented"
        assert match.target_title == "Implement analysis history selector"
        assert match.target_content == "Implement analysis history selector"
        assert match.target_type == "todo"

    def test_guard_skip_excluded_from_candidates(
        self, memory_db: Database,
    ) -> None:
        engine, extractor = _setup_engine_and_extractor(memory_db)
        engine.start_session()

        todo_id = self._insert_entity(
            memory_db, engine.project_id, "todo",
            "Implement analysis history selector", status="open",
        )
        short_id = todo_id[-8:]
        discussion_event = engine.ingest_one(
            "response",
            f"Triage note: #{short_id} still open, discussed with team.",
        )
        assert discussion_event is not None
        self._insert_entity(
            memory_db, engine.project_id, "feature",
            "Analysis history selector implemented",
            source_event_ids=[discussion_event.id],
        )

        stats: dict[str, int] = {}
        candidates = extractor.gather_resolution_candidates(
            engine.project_id, stats=stats,
        )
        assert not any(c.target_id == todo_id for c in candidates)
        assert stats.get("skipped_by_guard") == 1

    def test_no_drivers_returns_empty_list(
        self, memory_db: Database,
    ) -> None:
        engine, extractor = _setup_engine_and_extractor(memory_db)
        self._insert_entity(
            memory_db, engine.project_id, "todo",
            "Something lonely", status="open",
        )
        candidates = extractor.gather_resolution_candidates(engine.project_id)
        assert candidates == []

    def test_stale_driver_ignored(self, memory_db: Database) -> None:
        engine, extractor = _setup_engine_and_extractor(memory_db)
        self._insert_entity(
            memory_db, engine.project_id, "feature",
            "Analysis history selector implemented", stale=1,
        )
        todo_id = self._insert_entity(
            memory_db, engine.project_id, "todo",
            "Implement analysis history selector", status="open",
        )
        candidates = extractor.gather_resolution_candidates(engine.project_id)
        assert not any(c.target_id == todo_id for c in candidates)


class TestWidenRecallWithEmbeddings:
    """Embedding similarity optionally widens the judged sweep's recall
    past what keyword matching alone would find -- e.g. a paraphrased
    completion sharing no words with its TODO's title. Degrades cleanly
    to keyword-only whenever vector data is unusable."""

    class _StubEmbedder:
        """Deterministic embedder stand-in -- returns the same fixed
        vector for every text, so tests control similarity directly.
        Records every ``embed()`` call's text batch so tests can assert
        on chunking (or on it never being called at all)."""

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

    @staticmethod
    def _setup(memory_db: Database, config: Config):
        from callmem.core.engine import MemoryEngine

        engine = MemoryEngine(memory_db, config)
        extractor = EntityExtractor(memory_db, OllamaClient(), config=config)
        return engine, extractor

    def test_embeddings_disabled_falls_back_to_keyword_only(
        self, memory_db: Database,
    ) -> None:
        config = Config(
            sensitive_data={"enabled": False, "llm_scan": False},
            embeddings={"enabled": False},
        )
        engine, extractor = self._setup(memory_db, config)

        TestGatherResolutionCandidates._insert_entity(
            memory_db, engine.project_id, "todo",
            "Investigate login bug", status="open",
        )
        TestGatherResolutionCandidates._insert_entity(
            memory_db, engine.project_id, "feature",
            "Refactored the auth module",
        )

        candidates = extractor.gather_resolution_candidates(
            engine.project_id, embedder=self._StubEmbedder([1.0, 0.0, 0.0]),
        )
        # No keyword overlap between the two titles and embeddings are
        # off -- nothing should surface.
        assert candidates == []

    def test_similar_vector_widens_recall_past_keyword_miss(
        self, memory_db: Database,
    ) -> None:
        from callmem.core.embeddings import embedding_model_key, pack_vector
        from callmem.core.repository import Repository

        config = Config(sensitive_data={"enabled": False, "llm_scan": False})
        engine, extractor = self._setup(memory_db, config)
        repo = Repository(memory_db)

        todo_id = TestGatherResolutionCandidates._insert_entity(
            memory_db, engine.project_id, "todo",
            "Investigate login bug", status="open",
        )
        vector = [1.0, 0.0, 0.0]
        model_key = embedding_model_key(config)
        repo.upsert_embedding(
            todo_id, model_key, len(vector), pack_vector(vector),
        )
        driver_id = TestGatherResolutionCandidates._insert_entity(
            memory_db, engine.project_id, "feature",
            "Refactored the auth module",
        )

        candidates = extractor.gather_resolution_candidates(
            engine.project_id, embedder=self._StubEmbedder(vector),
        )
        assert any(
            c.target_id == todo_id and c.driver_id == driver_id
            for c in candidates
        )

    def test_guard_applies_to_embedding_widened_candidates(
        self, memory_db: Database,
    ) -> None:
        from callmem.core.embeddings import embedding_model_key, pack_vector
        from callmem.core.repository import Repository

        config = Config(sensitive_data={"enabled": False, "llm_scan": False})
        engine, extractor = self._setup(memory_db, config)
        engine.start_session()
        repo = Repository(memory_db)

        todo_id = TestGatherResolutionCandidates._insert_entity(
            memory_db, engine.project_id, "todo",
            "Investigate login bug", status="open",
        )
        short_id = todo_id[-8:]
        vector = [1.0, 0.0, 0.0]
        model_key = embedding_model_key(config)
        repo.upsert_embedding(
            todo_id, model_key, len(vector), pack_vector(vector),
        )

        discussion_event = engine.ingest_one(
            "response",
            f"Triage note: #{short_id} still open, discussed with team.",
        )
        assert discussion_event is not None
        TestGatherResolutionCandidates._insert_entity(
            memory_db, engine.project_id, "feature",
            "Refactored the auth module",
            source_event_ids=[discussion_event.id],
        )

        stats: dict[str, int] = {}
        candidates = extractor.gather_resolution_candidates(
            engine.project_id, stats=stats,
            embedder=self._StubEmbedder(vector),
        )
        assert not any(c.target_id == todo_id for c in candidates)
        assert stats.get("skipped_by_guard") == 1

    def test_disabled_with_stored_embeddings_never_calls_embedder(
        self, memory_db: Database,
    ) -> None:
        """Vectors already exist for this project, but embeddings are
        disabled in config -- widen must short-circuit before ever
        touching the embedder (a project that turned embeddings off
        should not pay for or use stale vector data)."""
        from callmem.core.embeddings import embedding_model_key, pack_vector
        from callmem.core.repository import Repository

        config = Config(
            sensitive_data={"enabled": False, "llm_scan": False},
            embeddings={"enabled": False},
        )
        engine, extractor = self._setup(memory_db, config)
        repo = Repository(memory_db)

        todo_id = TestGatherResolutionCandidates._insert_entity(
            memory_db, engine.project_id, "todo",
            "Investigate login bug", status="open",
        )
        vector = [1.0, 0.0, 0.0]
        model_key = embedding_model_key(config)
        repo.upsert_embedding(
            todo_id, model_key, len(vector), pack_vector(vector),
        )
        TestGatherResolutionCandidates._insert_entity(
            memory_db, engine.project_id, "feature",
            "Refactored the auth module",
        )

        embedder = self._StubEmbedder(vector)
        candidates = extractor.gather_resolution_candidates(
            engine.project_id, embedder=embedder,
        )
        assert candidates == []
        assert embedder.calls == []

    def test_embed_call_is_chunked_by_batch_size(
        self, memory_db: Database,
    ) -> None:
        config = Config(
            sensitive_data={"enabled": False, "llm_scan": False},
            embeddings={"batch_size": 2},
        )
        engine, extractor = self._setup(memory_db, config)

        from callmem.core.embeddings import embedding_model_key, pack_vector
        from callmem.core.repository import Repository

        repo = Repository(memory_db)
        vector = [1.0, 0.0, 0.0]
        model_key = embedding_model_key(config)
        todo_id = TestGatherResolutionCandidates._insert_entity(
            memory_db, engine.project_id, "todo",
            "Investigate login bug", status="open",
        )
        repo.upsert_embedding(
            todo_id, model_key, len(vector), pack_vector(vector),
        )
        # 5 drivers, batch_size=2 -- must be embedded in ceil(5/2)=3 calls.
        for i in range(5):
            TestGatherResolutionCandidates._insert_entity(
                memory_db, engine.project_id, "feature",
                f"Unrelated driver number {i}",
            )

        embedder = self._StubEmbedder(vector)
        extractor.gather_resolution_candidates(
            engine.project_id, embedder=embedder,
        )

        assert len(embedder.calls) == 3
        assert [len(c) for c in embedder.calls] == [2, 2, 1]
        assert sum(len(c) for c in embedder.calls) == 5

    def test_widen_threshold_stricter_than_global_search_floor(
        self, memory_db: Database,
    ) -> None:
        """A vector match between the global search floor (0.45 default)
        and the widen-specific floor must NOT widen -- widen candidates
        cost judge calls, so the bar is deliberately higher than plain
        retrieval."""
        from callmem.core.embeddings import embedding_model_key, pack_vector
        from callmem.core.repository import Repository

        config = Config(sensitive_data={"enabled": False, "llm_scan": False})
        assert config.embeddings.min_similarity < 0.60
        engine, extractor = self._setup(memory_db, config)
        repo = Repository(memory_db)

        todo_id = TestGatherResolutionCandidates._insert_entity(
            memory_db, engine.project_id, "todo",
            "Investigate login bug", status="open",
        )
        # cosine([1,0,0], [1,1.5,0]) == 1/sqrt(3.25) ~= 0.555 -- above the
        # global search floor (0.45 default) but below the widen floor.
        target_vector = [1.0, 1.5, 0.0]
        model_key = embedding_model_key(config)
        repo.upsert_embedding(
            todo_id, model_key, len(target_vector), pack_vector(target_vector),
        )
        TestGatherResolutionCandidates._insert_entity(
            memory_db, engine.project_id, "feature",
            "Refactored the auth module",
        )

        query_vector = [1.0, 0.0, 0.0]
        import math

        cosine = query_vector[0] * target_vector[0] / (
            math.sqrt(sum(x * x for x in query_vector))
            * math.sqrt(sum(x * x for x in target_vector))
        )
        assert config.embeddings.min_similarity < cosine < 0.60

        candidates = extractor.gather_resolution_candidates(
            engine.project_id, embedder=self._StubEmbedder(query_vector),
        )
        assert not any(c.target_id == todo_id for c in candidates)

    def test_global_pair_cap_keeps_highest_similarity_first(
        self, memory_db: Database, caplog,
    ) -> None:
        import logging

        from callmem.core.embeddings import embedding_model_key, pack_vector
        from callmem.core.repository import Repository

        config = Config(sensitive_data={"enabled": False, "llm_scan": False})
        engine, extractor = self._setup(memory_db, config)
        extractor._EMBED_WIDEN_MAX_PAIRS = 2  # instance override, don't need 50+ rows
        repo = Repository(memory_db)
        model_key = embedding_model_key(config)

        # Three targets at descending similarity to the driver's query
        # vector [1, 0, 0] -- all above the widen floor, so all three
        # would qualify without the cap.
        todo_high = TestGatherResolutionCandidates._insert_entity(
            memory_db, engine.project_id, "todo", "Investigate login bug A",
            status="open",
        )
        repo.upsert_embedding(
            todo_high, model_key, 3, pack_vector([1.0, 0.0, 0.0]),
        )
        todo_mid = TestGatherResolutionCandidates._insert_entity(
            memory_db, engine.project_id, "todo", "Investigate login bug B",
            status="open",
        )
        repo.upsert_embedding(
            todo_mid, model_key, 3, pack_vector([1.0, 0.3, 0.0]),
        )
        todo_low = TestGatherResolutionCandidates._insert_entity(
            memory_db, engine.project_id, "todo", "Investigate login bug C",
            status="open",
        )
        repo.upsert_embedding(
            todo_low, model_key, 3, pack_vector([1.0, 0.8, 0.0]),
        )
        TestGatherResolutionCandidates._insert_entity(
            memory_db, engine.project_id, "feature",
            "Refactored the auth module",
        )

        with caplog.at_level(logging.INFO):
            candidates = extractor.gather_resolution_candidates(
                engine.project_id,
                embedder=self._StubEmbedder([1.0, 0.0, 0.0]),
            )

        target_ids = {c.target_id for c in candidates}
        assert target_ids == {todo_high, todo_mid}
        assert todo_low not in target_ids
        assert any("capping" in r.message.lower() for r in caplog.records)
