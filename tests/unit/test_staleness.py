"""Tests for staleness detection."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from click.testing import CliRunner

from callmem.cli import main
from callmem.core.config import load_config
from callmem.core.database import Database
from callmem.core.engine import MemoryEngine
from callmem.core.retrieval import RetrievalEngine
from callmem.core.staleness import StalenessChecker, _fts_query_from
from callmem.mcp.tools import (
    handle_mark_current,
    handle_mark_stale,
    handle_search,
)
from callmem.models.entities import Entity

if TYPE_CHECKING:
    from pathlib import Path

    from callmem.core.repository import Repository


# ── Helpers ──────────────────────────────────────────────────────────


def _hours_ago(hours: float) -> str:
    """ISO-8601 UTC timestamp N hours before test run.

    Using relative dates (instead of hardcoded ``2026-04-19T…``) keeps the
    staleness tests from rotting every time the real clock moves past the
    StalenessChecker default lookback window.
    """
    moment = datetime.now(timezone.utc) - timedelta(hours=hours)
    return moment.isoformat()


def _make_engine(project: Path) -> MemoryEngine:
    runner = CliRunner()
    result = runner.invoke(main, ["init", "--project", str(project)])
    assert result.exit_code == 0
    config = load_config(project)
    db = Database(project / ".callmem" / "memory.db")
    db.initialize()
    return MemoryEngine(db, config)


def _insert_entity(
    repo: Repository,
    project_id: str,
    etype: str,
    title: str,
    content: str,
    created_at: str | None = None,
    entity_id: str | None = None,
) -> str:
    if created_at is None:
        # Default to "2 hours ago" so entities land well inside any
        # reasonable lookback window regardless of test run time.
        created_at = _hours_ago(2)
    kwargs: dict = {
        "project_id": project_id, "type": etype,
        "title": title, "content": content,
        "created_at": created_at, "updated_at": created_at,
    }
    if entity_id is not None:
        kwargs["id"] = entity_id
    entity = Entity(**kwargs)
    row = entity.to_row()
    conn = repo.db.connect()
    try:
        conn.execute(
            "INSERT INTO entities "
            "(id, project_id, source_event_id, type, title, content, "
            "key_points, synopsis, status, priority, pinned, "
            "created_at, updated_at, resolved_at, metadata, archived_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row["id"], row["project_id"], row["source_event_id"],
                row["type"], row["title"], row["content"],
                row["key_points"], row["synopsis"], row["status"],
                row["priority"], row["pinned"], row["created_at"],
                row["updated_at"], row["resolved_at"], row["metadata"],
                row["archived_at"],
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return entity.id


# ── Schema / model ───────────────────────────────────────────────────


class TestSchemaV7:
    def test_new_columns_exist(self, tmp_path: Path) -> None:
        db = Database(tmp_path / "mem.db")
        db.initialize()
        assert db.get_schema_version() == 22
        conn = db.connect()
        try:
            cols = [r["name"] for r in conn.execute("PRAGMA table_info(entities)")]
        finally:
            conn.close()
        assert "stale" in cols
        assert "superseded_by" in cols
        assert "staleness_reason" in cols

    def test_insert_defaults_to_not_stale(self, tmp_path: Path) -> None:
        engine = _make_engine(tmp_path)
        eid = _insert_entity(
            engine.repo, engine.project_id,
            "decision", "auth uses JWT", "rs256 keypair rotated quarterly",
        )
        row = engine.repo.get_entity(eid)
        assert row is not None
        assert row["stale"] == 0
        assert row["superseded_by"] is None
        assert row["staleness_reason"] is None


# ── Manual marking ───────────────────────────────────────────────────


class TestManualMarking:
    def test_mark_stale_sets_flag_and_reason(self, tmp_path: Path) -> None:
        engine = _make_engine(tmp_path)
        eid = _insert_entity(
            engine.repo, engine.project_id,
            "fact", "legacy fact", "replaced later",
        )
        result = engine.mark_stale(eid, reason="manual", superseded_by="other")
        assert result["stale"] == 1
        assert result["staleness_reason"] == "manual"
        assert result["superseded_by"] == "other"

    def test_mark_current_clears_flag(self, tmp_path: Path) -> None:
        engine = _make_engine(tmp_path)
        eid = _insert_entity(
            engine.repo, engine.project_id,
            "fact", "maybe stale", "content",
        )
        engine.mark_stale(eid, reason="outdated")
        result = engine.mark_current(eid)
        assert result["stale"] == 0
        assert result["staleness_reason"] is None
        assert result["superseded_by"] is None

    def test_list_stale_returns_only_stale(self, tmp_path: Path) -> None:
        engine = _make_engine(tmp_path)
        e1 = _insert_entity(
            engine.repo, engine.project_id, "fact", "stale one", "x",
        )
        _insert_entity(
            engine.repo, engine.project_id, "fact", "current one", "y",
        )
        engine.mark_stale(e1, reason="superseded")
        stale = engine.list_stale_entities()
        assert len(stale) == 1
        assert stale[0]["id"] == e1


# ── Read filters ─────────────────────────────────────────────────────


class TestReadFilters:
    def test_get_entities_excludes_stale_by_default(
        self, tmp_path: Path,
    ) -> None:
        engine = _make_engine(tmp_path)
        _insert_entity(
            engine.repo, engine.project_id, "fact", "keep", "content",
        )
        gone = _insert_entity(
            engine.repo, engine.project_id, "fact", "drop", "content",
        )
        engine.mark_stale(gone, reason="superseded")

        visible = engine.get_entities(type="fact")
        assert {e["title"] for e in visible} == {"keep"}

        all_of_them = engine.get_entities(type="fact", include_stale=True)
        assert {e["title"] for e in all_of_them} == {"keep", "drop"}

    def test_retrieval_search_excludes_stale(self, tmp_path: Path) -> None:
        engine = _make_engine(tmp_path)
        _insert_entity(
            engine.repo, engine.project_id,
            "decision", "auth uses JWT", "RS256 keypair",
        )
        stale_id = _insert_entity(
            engine.repo, engine.project_id,
            "decision", "auth uses oauth", "old plan",
        )
        engine.mark_stale(stale_id, reason="superseded")

        retriever = RetrievalEngine(engine.repo, engine.config)
        results = retriever.search(
            engine.project_id, query="auth", strategies=["entities"],
        )
        titles = [r.title for r in results]
        assert "auth uses JWT" in titles
        assert "auth uses oauth" not in titles

        results_all = retriever.search(
            engine.project_id, query="auth",
            include_stale=True, strategies=["entities"],
        )
        titles_all = [r.title for r in results_all]
        assert "auth uses oauth" in titles_all


# ── MCP tools ────────────────────────────────────────────────────────


class TestMcpTools:
    def test_mark_stale_tool(self, tmp_path: Path) -> None:
        engine = _make_engine(tmp_path)
        eid = _insert_entity(
            engine.repo, engine.project_id,
            "decision", "title", "content",
        )
        result = handle_mark_stale(engine, {
            "entity_id": eid, "reason": "superseded",
        })
        data = json.loads(result[0].text)
        assert data["stale"] is True
        assert data["reason"] == "superseded"

    def test_mark_current_tool(self, tmp_path: Path) -> None:
        engine = _make_engine(tmp_path)
        eid = _insert_entity(
            engine.repo, engine.project_id,
            "decision", "title", "content",
        )
        engine.mark_stale(eid, reason="outdated")
        result = handle_mark_current(engine, {"entity_id": eid})
        data = json.loads(result[0].text)
        assert data["stale"] is False

    def test_mark_stale_tool_accepts_short_id(self, tmp_path: Path) -> None:
        # Live-forensics defect: mem_mark_stale({"entity_id": "BD97K0C7"})
        # ("Entity not found") while mem_get_entities resolved the same
        # short ID fine. Every entity-id tool must share one resolver.
        engine = _make_engine(tmp_path)
        eid = _insert_entity(
            engine.repo, engine.project_id,
            "decision", "title", "content",
        )
        result = handle_mark_stale(engine, {
            "entity_id": eid[-8:], "reason": "superseded",
        })
        data = json.loads(result[0].text)
        assert data["entity_id"] == eid
        assert data["stale"] is True

    def test_mark_stale_tool_unknown_id_is_loud_error(
        self, tmp_path: Path,
    ) -> None:
        engine = _make_engine(tmp_path)
        result = handle_mark_stale(engine, {
            "entity_id": "totallyunknown", "reason": "manual",
        })
        data = json.loads(result[0].text)
        assert "totallyunknown" in data["error"]

    def test_mark_stale_tool_ambiguous_short_id_is_loud_error(
        self, tmp_path: Path,
    ) -> None:
        engine = _make_engine(tmp_path)
        e1 = _insert_entity(
            engine.repo, engine.project_id,
            "decision", "one", "content",
            entity_id="AAAAAAAAAAAAAAAAAA01234567",
        )
        e2 = _insert_entity(
            engine.repo, engine.project_id,
            "decision", "two", "content",
            entity_id="BBBBBBBBBBBBBBBBBB01234567",
        )
        result = handle_mark_stale(engine, {
            "entity_id": "01234567", "reason": "manual",
        })
        data = json.loads(result[0].text)
        assert e1 in data["error"]
        assert e2 in data["error"]

    def test_mark_current_tool_accepts_short_id(self, tmp_path: Path) -> None:
        engine = _make_engine(tmp_path)
        eid = _insert_entity(
            engine.repo, engine.project_id,
            "decision", "title", "content",
        )
        engine.mark_stale(eid, reason="outdated")
        result = handle_mark_current(engine, {"entity_id": eid[-8:]})
        data = json.loads(result[0].text)
        assert data["entity_id"] == eid
        assert data["stale"] is False

    def test_mark_current_tool_unknown_id_is_loud_error(
        self, tmp_path: Path,
    ) -> None:
        engine = _make_engine(tmp_path)
        result = handle_mark_current(engine, {"entity_id": "totallyunknown"})
        data = json.loads(result[0].text)
        assert "totallyunknown" in data["error"]

    def test_search_honours_include_stale(self, tmp_path: Path) -> None:
        engine = _make_engine(tmp_path)
        _insert_entity(
            engine.repo, engine.project_id,
            "decision", "keep current", "uses Redis",
        )
        stale = _insert_entity(
            engine.repo, engine.project_id,
            "decision", "keep stale", "uses Redis",
        )
        engine.mark_stale(stale, reason="superseded")

        excluded = json.loads(handle_search(engine, {"query": "Redis"})[0].text)
        titles = {r["title"] for r in excluded["results"]}
        assert "keep stale" not in titles

        included = json.loads(handle_search(engine, {
            "query": "Redis", "include_stale": True,
        })[0].text)
        titles_all = {r["title"] for r in included["results"]}
        assert "keep stale" in titles_all


# ── FTS helper ───────────────────────────────────────────────────────


class TestFtsQuery:
    def test_filters_short_tokens_and_punctuation(self) -> None:
        q = _fts_query_from("Auth uses JWT-RS256!", "legacy")
        assert "auth" in q
        assert "jwt-rs256" in q or "jwt" in q or "rs256" in q
        assert "!" not in q

    def test_returns_empty_for_empty_inputs(self) -> None:
        assert _fts_query_from("", "") == ""


# ── Automatic detection ──────────────────────────────────────────────


class _StubLLM:
    """Deterministic LLM stand-in for staleness tests."""

    def __init__(self, verdict: str = "superseded") -> None:
        self.verdict = verdict
        self.calls: list[str] = []

    def extract(self, prompt: str) -> str:
        self.calls.append(prompt)
        return json.dumps({
            "verdict": self.verdict, "reason": "stubbed",
        })


class TestAutomaticDetection:
    def test_marks_older_entity_stale_when_superseded(
        self, tmp_path: Path,
    ) -> None:
        engine = _make_engine(tmp_path)
        # Older entity — still inside the 24h lookback window.
        older = _insert_entity(
            engine.repo, engine.project_id,
            "decision",
            "auth uses JWT tokens",
            "tokens signed with RS256 keypair rotated quarterly",
            created_at=_hours_ago(12),
        )
        # Newer entity — also inside the window, but more recent.
        _insert_entity(
            engine.repo, engine.project_id,
            "decision",
            "auth uses session cookies",
            "cookie-backed sessions, replaced JWT auth approach",
            created_at=_hours_ago(1),
        )

        llm = _StubLLM(verdict="superseded")
        checker = StalenessChecker(engine.db, llm, lookback_minutes=24 * 60)
        decisions = checker.run(engine.project_id)

        # The stub returned "superseded" — older row should be stale.
        assert any(d.older_id == older for d in decisions)
        row = engine.repo.get_entity(older)
        assert row is not None
        assert row["stale"] == 1
        assert row["staleness_reason"] == "superseded"

    def test_coexists_verdict_does_not_mark_stale(
        self, tmp_path: Path,
    ) -> None:
        engine = _make_engine(tmp_path)
        older = _insert_entity(
            engine.repo, engine.project_id,
            "fact", "queue backend is Redis", "used for background jobs",
            created_at=_hours_ago(12),
        )
        _insert_entity(
            engine.repo, engine.project_id,
            "fact", "Redis serves caching layer", "distinct use case",
            created_at=_hours_ago(1),
        )
        llm = _StubLLM(verdict="coexists")
        checker = StalenessChecker(engine.db, llm, lookback_minutes=24 * 60)
        checker.run(engine.project_id)
        assert engine.repo.get_entity(older)["stale"] == 0

    def test_skips_ineligible_types(self, tmp_path: Path) -> None:
        engine = _make_engine(tmp_path)
        _insert_entity(
            engine.repo, engine.project_id,
            "change", "bumped dep", "bumped click version",
            created_at=_hours_ago(1),
        )
        llm = _StubLLM(verdict="superseded")
        checker = StalenessChecker(engine.db, llm, lookback_minutes=24 * 60)
        checker.run(engine.project_id)
        assert llm.calls == []

    def test_no_llm_returns_no_decisions(self, tmp_path: Path) -> None:
        engine = _make_engine(tmp_path)
        _insert_entity(
            engine.repo, engine.project_id,
            "decision", "a title", "some content",
            created_at=_hours_ago(1),
        )
        checker = StalenessChecker(engine.db, ollama=None)
        assert checker.run(engine.project_id) == []


# ── Malformed LLM responses (live-forensics) ────────────────────────


class _MalformedLLM:
    """Stand-in returning a fixed raw response, unlike ``_StubLLM``
    which always returns well-formed JSON."""

    def __init__(self, raw: str) -> None:
        self.raw = raw
        self.calls: list[str] = []

    def extract(self, prompt: str) -> str:
        self.calls.append(prompt)
        return self.raw


class TestMalformedLLMResponse:
    """llm-mem production had 46 failed staleness_check jobs, all with
    ``Expecting value: line 1 column 1 (char 0)`` -- a json.loads on
    text that isn't valid JSON. Live-captured against the real
    OpenRouter backend (google/gemma-3-27b-it) for a real llm-mem
    entity pair, the model sometimes prefixes the fenced JSON block
    with stray preamble text (e.g. a stray "SS" or ":" token) before
    the ```json fence. ``strip_code_fences`` only strips a leading
    fence when the string STARTS with ```, so the preamble survives
    and json.loads chokes on it at char 0 -- reproducing the exact
    production error. These entries must never crash the job or mark
    anything stale on unparseable output.
    """

    def test_prose_prefixed_fenced_json_does_not_crash_job(
        self, tmp_path: Path,
    ) -> None:
        engine = _make_engine(tmp_path)
        older = _insert_entity(
            engine.repo, engine.project_id,
            "fact", "old fact", "content",
            created_at=_hours_ago(12),
        )
        _insert_entity(
            engine.repo, engine.project_id,
            "fact", "new fact", "content",
            created_at=_hours_ago(1),
        )
        # Captured verbatim from OpenRouter against a real llm-mem pair.
        raw = (
            ':\n```json\n'
            '{"verdict": "superseded", "reason": "..."}\n'
            '```'
        )
        llm = _MalformedLLM(raw)
        checker = StalenessChecker(engine.db, llm, lookback_minutes=24 * 60)

        decisions = checker.run(engine.project_id)  # must not raise

        assert decisions == []
        row = engine.repo.get_entity(older)
        assert row is not None
        assert row["stale"] == 0

    def test_empty_fenced_block_does_not_crash_job(
        self, tmp_path: Path,
    ) -> None:
        # A model can also emit an empty fenced block (e.g. on a
        # refusal or truncated generation); after fence-stripping this
        # leaves an empty string, which json.loads rejects with the
        # same "Expecting value" error.
        engine = _make_engine(tmp_path)
        older = _insert_entity(
            engine.repo, engine.project_id,
            "fact", "old fact", "content",
            created_at=_hours_ago(12),
        )
        _insert_entity(
            engine.repo, engine.project_id,
            "fact", "new fact", "content",
            created_at=_hours_ago(1),
        )
        llm = _MalformedLLM("```json\n```")
        checker = StalenessChecker(engine.db, llm, lookback_minutes=24 * 60)

        decisions = checker.run(engine.project_id)  # must not raise

        assert decisions == []
        row = engine.repo.get_entity(older)
        assert row is not None
        assert row["stale"] == 0


# ── Briefing footer ──────────────────────────────────────────────────


class TestBriefingFooter:
    def test_stale_count_in_briefing(self, tmp_path: Path) -> None:
        from callmem.core.briefing import BriefingGenerator

        engine = _make_engine(tmp_path)
        _insert_entity(
            engine.repo, engine.project_id, "fact", "keep", "x",
        )
        gone = _insert_entity(
            engine.repo, engine.project_id, "fact", "drop", "y",
        )
        engine.mark_stale(gone, reason="superseded")

        gen = BriefingGenerator(engine.repo, engine.config, ollama=None)
        briefing = gen.generate(engine.project_id)
        assert "stale" in briefing.content
        assert "suppressed" in briefing.content


# ── CLI ──────────────────────────────────────────────────────────────


class TestUIEndpoints:
    def test_stale_and_current_endpoints_round_trip(
        self, tmp_path: Path,
    ) -> None:
        from fastapi.testclient import TestClient

        from callmem.ui.app import create_app

        engine = _make_engine(tmp_path)
        eid = _insert_entity(
            engine.repo, engine.project_id,
            "decision", "title", "content",
        )
        app = create_app(engine)
        client = TestClient(app)

        r = client.post(f"/entities/{eid}/stale?reason=manual")
        assert r.status_code == 200
        assert "stale" in r.text

        row = engine.repo.get_entity(eid)
        assert row["stale"] == 1

        r = client.post(f"/entities/{eid}/current")
        assert r.status_code == 200
        assert "current" in r.text

        row = engine.repo.get_entity(eid)
        assert row["stale"] == 0

    def test_feed_partial_hides_stale_by_default(
        self, tmp_path: Path,
    ) -> None:
        from fastapi.testclient import TestClient

        from callmem.ui.app import create_app

        engine = _make_engine(tmp_path)
        _insert_entity(
            engine.repo, engine.project_id,
            "decision", "current title",
            "current content for visibility",
        )
        stale_id = _insert_entity(
            engine.repo, engine.project_id,
            "decision", "stale title",
            "superseded content for visibility",
        )
        engine.mark_stale(stale_id, reason="superseded")
        client = TestClient(create_app(engine))

        hidden = client.get("/partials/feed").text
        assert "current title" in hidden
        assert "stale title" not in hidden

        shown = client.get("/partials/feed?include_stale=true").text
        assert "stale title" in shown
        assert "is-stale" in shown
        assert "feed-card-stale" in shown


class TestCli:
    def test_stale_list_empty(self, tmp_path: Path) -> None:
        runner = CliRunner()
        runner.invoke(main, ["init", "--project", str(tmp_path)])
        result = runner.invoke(main, ["stale", "--project", str(tmp_path)])
        assert result.exit_code == 0
        assert "No stale" in result.output

    def test_stale_reset(self, tmp_path: Path) -> None:
        engine = _make_engine(tmp_path)
        eid = _insert_entity(
            engine.repo, engine.project_id,
            "fact", "x", "y",
        )
        engine.mark_stale(eid, reason="manual")

        runner = CliRunner()
        result = runner.invoke(main, [
            "stale", "--project", str(tmp_path), "--reset", eid,
        ])
        assert result.exit_code == 0
        assert "stale=False" in result.output

    def test_stale_list_flags_contradiction_invalidated_entries(
        self, tmp_path: Path,
    ) -> None:
        """A contradiction-invalidated entry (invalidated_at set, as done
        by consolidation's CONTRADICTS verdict) must be visibly
        distinguished from an ordinary stale entry in the listing."""
        engine = _make_engine(tmp_path)
        plain_id = _insert_entity(
            engine.repo, engine.project_id, "fact", "plain stale", "x",
        )
        contradicted_id = _insert_entity(
            engine.repo, engine.project_id, "fact", "contradicted stale", "y",
        )
        engine.repo.mark_stale(plain_id, reason="superseded")
        engine.repo.mark_stale(
            contradicted_id, reason="contradicted",
            superseded_by="other", invalidated=True,
        )

        runner = CliRunner()
        result = runner.invoke(main, ["stale", "--project", str(tmp_path)])
        assert result.exit_code == 0

        plain_line = next(
            line for line in result.output.splitlines()
            if "plain stale" in line
        )
        contradicted_line = next(
            line for line in result.output.splitlines()
            if "contradicted stale" in line
        )
        # Both lines mention their own staleness_reason text ("superseded"
        # vs "contradicted"), but only the contradiction-invalidated one
        # additionally surfaces its invalidated_at timestamp.
        assert "invalidated" not in plain_line
        assert "invalidated" in contradicted_line
