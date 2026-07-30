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
