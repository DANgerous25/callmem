"""Tests for embedding infrastructure and hybrid retrieval."""

from __future__ import annotations

import math
import time
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest

from callmem.core.database import Database
from callmem.core.embeddings import (
    EMBED_JOB_TYPE,
    EntityEmbedder,
    OllamaEmbedder,
    OpenAICompatEmbedder,
    cosine_similarity,
    create_embedder,
    entity_embedding_text,
    pack_vector,
    rank_by_similarity,
    unpack_vector,
)
from callmem.core.queue import JobQueue
from callmem.core.repository import Repository
from callmem.models.config import Config
from callmem.models.entities import Entity
from callmem.models.projects import Project

if TYPE_CHECKING:
    from pathlib import Path


# ── Helpers ──────────────────────────────────────────────────────────


class StubEmbedder:
    """Deterministic in-test embedder — no network."""

    def __init__(self, mapping: dict[str, list[float]], model: str = "stub-embed") -> None:
        self.mapping = mapping
        self.model = model
        self.dim = len(next(iter(mapping.values()))) if mapping else 3
        self.calls: list[list[str]] = []
        self.timeouts: list[float | None] = []

    def is_available(self) -> bool:
        return True

    def embed(
        self, texts: list[str], timeout: float | None = None,
    ) -> list[list[float]] | None:
        self.calls.append(list(texts))
        self.timeouts.append(timeout)
        out: list[list[float]] = []
        for t in texts:
            for key, vec in self.mapping.items():
                if key.lower() in t.lower():
                    out.append(list(vec))
                    break
            else:
                out.append([0.0] * self.dim)
        return out


def _backend_up() -> Any:
    """Pretend the embedding backend answered its availability probe."""
    return patch(
        "callmem.core.embeddings.embedding_backend_available", return_value=True,
    )


def _backend_down() -> Any:
    return patch(
        "callmem.core.embeddings.embedding_backend_available", return_value=False,
    )


@pytest.fixture(autouse=True)
def _clear_embedding_caches() -> Any:
    """Availability and log-once state are process-wide — isolate tests."""
    from callmem.core.embeddings import reset_availability_cache
    from callmem.core.retrieval import reset_degradation_log

    reset_availability_cache()
    reset_degradation_log()
    yield
    reset_availability_cache()
    reset_degradation_log()


STUB_MODEL = "stub-embed"
#: What embedding_model_key() yields for _stub_config() below.
STUB_KEY = "stub-embed|"


def _stub_config(**overrides: Any) -> Config:
    """Config whose embedding identity matches StubEmbedder's."""
    embeddings: dict[str, Any] = {
        "model": STUB_MODEL, "document_prefix": "", "query_prefix": "",
    }
    embeddings.update(overrides)
    return Config(embeddings=embeddings)


def _insert_entity(db: Database, entity: Entity) -> None:
    conn = db.connect()
    try:
        row = entity.to_row()
        conn.execute(
            "INSERT INTO entities "
            "(id, project_id, source_event_id, type, title, content, "
            "key_points, synopsis, extracted_by, status, priority, pinned, "
            "created_at, updated_at, resolved_at, metadata, archived_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row["id"], row["project_id"], row["source_event_id"],
                row["type"], row["title"], row["content"],
                row["key_points"], row["synopsis"], row["extracted_by"],
                row["status"], row["priority"], row["pinned"],
                row["created_at"], row["updated_at"],
                row["resolved_at"], row["metadata"], row["archived_at"],
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_project(db: Database) -> str:
    repo = Repository(db)
    project = Project(name="embed-test")
    repo.create_project(project)
    return project.id


# ── 1b: vector packing ───────────────────────────────────────────────


class TestVectorPacking:
    def test_pack_unpack_roundtrip(self) -> None:
        values = [0.0, 1.0, -0.5, 0.25]
        blob = pack_vector(values)
        assert isinstance(blob, bytes)
        assert len(blob) == 4 * len(values)
        assert unpack_vector(blob) == pytest.approx(values)

    def test_pack_is_float32(self) -> None:
        # float32 loses precision beyond ~7 significant digits — proving
        # the packing really is 4-byte floats, not float64.
        blob = pack_vector([0.1234567890123])
        assert len(blob) == 4
        assert unpack_vector(blob)[0] != 0.1234567890123

    def test_cosine_similarity(self) -> None:
        assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
        assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
        assert cosine_similarity([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)

    def test_cosine_similarity_zero_vector_is_zero(self) -> None:
        assert cosine_similarity([0.0, 0.0], [1.0, 0.0]) == 0.0

    def test_cosine_similarity_length_mismatch_is_zero(self) -> None:
        assert cosine_similarity([1.0, 0.0], [1.0, 0.0, 0.0]) == 0.0

    def test_rank_by_similarity_orders_and_filters(self) -> None:
        candidates = [
            {"entity_id": "far", "dim": 2, "vector": pack_vector([0.0, 1.0])},
            {"entity_id": "near", "dim": 2, "vector": pack_vector([1.0, 0.0])},
            {"entity_id": "mid", "dim": 2, "vector": pack_vector([1.0, 1.0])},
        ]
        ranked = rank_by_similarity([1.0, 0.0], candidates, min_similarity=0.5)
        assert [eid for _s, eid in ranked] == ["near", "mid"]

    def test_rank_by_similarity_skips_dim_mismatch(self) -> None:
        candidates = [
            {"entity_id": "other", "dim": 3,
             "vector": pack_vector([1.0, 0.0, 0.0])},
            {"entity_id": "ok", "dim": 2, "vector": pack_vector([1.0, 0.0])},
        ]
        ranked = rank_by_similarity([1.0, 0.0], candidates, min_similarity=-1.0)
        assert [eid for _s, eid in ranked] == ["ok"]

    def test_rank_by_similarity_matches_cosine_similarity(self) -> None:
        """The hoisted-norm batch form must agree with the primitive."""
        vectors = [[0.3, -0.4, 0.5], [1.0, 2.0, -3.0], [0.0, 0.1, 0.0]]
        query = [0.2, 0.9, -0.1]
        candidates = [
            {"entity_id": str(i), "dim": 3, "vector": pack_vector(v)}
            for i, v in enumerate(vectors)
        ]
        ranked = dict(
            (eid, s)
            for s, eid in rank_by_similarity(query, candidates, -1.0)
        )
        for i, v in enumerate(vectors):
            assert ranked[str(i)] == pytest.approx(
                cosine_similarity(query, v), rel=1e-6,
            )

    def test_rank_by_similarity_zero_query_is_empty(self) -> None:
        candidates = [
            {"entity_id": "a", "dim": 2, "vector": pack_vector([1.0, 0.0])},
        ]
        assert rank_by_similarity([0.0, 0.0], candidates, -1.0) == []

    def test_entity_embedding_text_combines_fields(self) -> None:
        text = entity_embedding_text({
            "title": "Use RRF",
            "synopsis": "reciprocal rank fusion",
            "content": "fuse bm25 and cosine",
            "key_points": "• k=60",
        })
        assert "Use RRF" in text
        assert "reciprocal rank fusion" in text
        assert "fuse bm25 and cosine" in text

    def test_entity_embedding_text_is_truncated(self) -> None:
        text = entity_embedding_text(
            {"title": "t", "content": "x" * 10_000}, max_chars=500,
        )
        assert len(text) <= 500


# ── 1b: migration 018 + storage ──────────────────────────────────────


class TestEmbeddingsMigration:
    def test_embeddings_table_exists(self, memory_db: Database) -> None:
        assert "embeddings" in memory_db.list_tables()

    def test_schema_version_at_least_18(self, memory_db: Database) -> None:
        assert memory_db.get_schema_version() >= 18

    def test_migration_applies_to_populated_v17_database(
        self, tmp_path: Path,
    ) -> None:
        """Migration 018 must succeed on a DB already carrying data."""
        db = Database(tmp_path / "populated.db")
        # Apply everything up to 017 only.
        conn = db.connect()
        try:
            db._ensure_schema_version_table(conn)
            for version, sql in db._load_migrations():
                if version > 17:
                    continue
                conn.executescript(sql)
                conn.execute(
                    "INSERT INTO schema_version (version, applied_at, description) "
                    "VALUES (?, datetime('now'), ?)",
                    (version, f"Migration {version:03d}"),
                )
            conn.commit()
        finally:
            conn.close()

        project_id = _seed_project(db)
        _insert_entity(db, Entity(
            project_id=project_id, type="fact",
            title="pre-existing", content="row from before migration 018",
        ))

        db.initialize()

        assert db.get_schema_version() >= 18
        assert "embeddings" in db.list_tables()
        repo = Repository(db)
        assert len(repo.get_entities(project_id)) == 1

    def test_embedding_deleted_with_entity(self, memory_db: Database) -> None:
        project_id = _seed_project(memory_db)
        entity = Entity(project_id=project_id, type="fact", title="t", content="c")
        _insert_entity(memory_db, entity)
        repo = Repository(memory_db)
        repo.upsert_embedding(entity.id, "m", 3, pack_vector([1.0, 0.0, 0.0]))

        conn = memory_db.connect()
        try:
            conn.execute("DELETE FROM entities WHERE id = ?", (entity.id,))
            conn.commit()
        finally:
            conn.close()

        assert repo.get_embedding(entity.id) is None


class TestEmbeddingRepository:
    def test_upsert_and_get(self, memory_db: Database) -> None:
        project_id = _seed_project(memory_db)
        entity = Entity(project_id=project_id, type="fact", title="t", content="c")
        _insert_entity(memory_db, entity)
        repo = Repository(memory_db)

        repo.upsert_embedding(entity.id, "stub", 3, pack_vector([1.0, 0.0, 0.0]))
        row = repo.get_embedding(entity.id)
        assert row is not None
        assert row["model"] == "stub"
        assert row["dim"] == 3
        assert unpack_vector(row["vector"]) == pytest.approx([1.0, 0.0, 0.0])

    def test_upsert_replaces_existing(self, memory_db: Database) -> None:
        project_id = _seed_project(memory_db)
        entity = Entity(project_id=project_id, type="fact", title="t", content="c")
        _insert_entity(memory_db, entity)
        repo = Repository(memory_db)

        repo.upsert_embedding(entity.id, "old", 3, pack_vector([1.0, 0.0, 0.0]))
        repo.upsert_embedding(entity.id, "new", 3, pack_vector([0.0, 1.0, 0.0]))

        row = repo.get_embedding(entity.id)
        assert row["model"] == "new"
        assert unpack_vector(row["vector"]) == pytest.approx([0.0, 1.0, 0.0])
        assert repo.count_embeddings(project_id) == 1

    def test_has_embeddings_is_project_scoped(self, memory_db: Database) -> None:
        repo = Repository(memory_db)
        p1 = _seed_project(memory_db)
        p2 = Project(name="other")
        repo.create_project(p2)

        e1 = Entity(project_id=p1, type="fact", title="a", content="a")
        _insert_entity(memory_db, e1)
        assert repo.has_embeddings(p1) is False

        repo.upsert_embedding(e1.id, "stub", 3, pack_vector([1.0, 0.0, 0.0]))
        assert repo.has_embeddings(p1) is True
        assert repo.has_embeddings(p2.id) is False

    def test_list_entities_missing_embeddings(self, memory_db: Database) -> None:
        project_id = _seed_project(memory_db)
        repo = Repository(memory_db)
        kept = Entity(project_id=project_id, type="fact", title="kept", content="c1")
        done = Entity(project_id=project_id, type="fact", title="done", content="c2")
        archived = Entity(
            project_id=project_id, type="fact", title="archived", content="c3",
        )
        for e in (kept, done, archived):
            _insert_entity(memory_db, e)
        repo.upsert_embedding(done.id, "stub", 3, pack_vector([1.0, 0.0, 0.0]))
        conn = memory_db.connect()
        try:
            conn.execute(
                "UPDATE entities SET archived_at = datetime('now') WHERE id = ?",
                (archived.id,),
            )
            conn.commit()
        finally:
            conn.close()

        missing = repo.list_entities_missing_embeddings(project_id, "stub", limit=10)
        assert [m["id"] for m in missing] == [kept.id]

    def test_missing_embeddings_includes_other_model(
        self, memory_db: Database,
    ) -> None:
        """Switching embedding model makes existing rows re-embeddable."""
        project_id = _seed_project(memory_db)
        repo = Repository(memory_db)
        entity = Entity(project_id=project_id, type="fact", title="t", content="c")
        _insert_entity(memory_db, entity)
        repo.upsert_embedding(entity.id, "old-model", 3, pack_vector([1.0, 0.0, 0.0]))

        assert repo.list_entities_missing_embeddings(project_id, "old-model") == []
        missing = repo.list_entities_missing_embeddings(project_id, "new-model")
        assert [m["id"] for m in missing] == [entity.id]

    def test_load_embedding_candidates_respects_cap_and_filters(
        self, memory_db: Database,
    ) -> None:
        project_id = _seed_project(memory_db)
        repo = Repository(memory_db)
        ids: list[str] = []
        for i in range(5):
            e = Entity(project_id=project_id, type="fact", title=f"t{i}", content="c")
            _insert_entity(memory_db, e)
            repo.upsert_embedding(e.id, "stub", 3, pack_vector([1.0, 0.0, 0.0]))
            ids.append(e.id)

        cands = repo.load_embedding_candidates(project_id, "stub", limit=3)
        assert len(cands) == 3
        assert all("vector" in c and "entity_id" in c for c in cands)

    def test_load_embedding_candidates_excludes_stale_and_archived(
        self, memory_db: Database,
    ) -> None:
        project_id = _seed_project(memory_db)
        repo = Repository(memory_db)
        good = Entity(project_id=project_id, type="fact", title="good", content="c")
        stale = Entity(project_id=project_id, type="fact", title="stale", content="c")
        arch = Entity(project_id=project_id, type="fact", title="arch", content="c")
        for e in (good, stale, arch):
            _insert_entity(memory_db, e)
            repo.upsert_embedding(e.id, "stub", 3, pack_vector([1.0, 0.0, 0.0]))
        conn = memory_db.connect()
        try:
            conn.execute("UPDATE entities SET stale = 1 WHERE id = ?", (stale.id,))
            conn.execute(
                "UPDATE entities SET archived_at = datetime('now') WHERE id = ?",
                (arch.id,),
            )
            conn.commit()
        finally:
            conn.close()

        ids = {c["entity_id"] for c in repo.load_embedding_candidates(project_id, "stub")}
        assert ids == {good.id}

        ids_stale = {
            c["entity_id"]
            for c in repo.load_embedding_candidates(
                project_id, "stub", include_stale=True,
            )
        }
        assert ids_stale == {good.id, stale.id}

    def test_get_entities_by_ids(self, memory_db: Database) -> None:
        project_id = _seed_project(memory_db)
        repo = Repository(memory_db)
        a = Entity(project_id=project_id, type="fact", title="a", content="c")
        b = Entity(project_id=project_id, type="todo", title="b", content="c")
        _insert_entity(memory_db, a)
        _insert_entity(memory_db, b)

        rows = repo.get_entities_by_ids([a.id, b.id])
        assert {r["id"] for r in rows} == {a.id, b.id}

        typed = repo.get_entities_by_ids([a.id, b.id], types=["todo"])
        assert [r["id"] for r in typed] == [b.id]

    def test_get_entities_by_ids_empty(self, memory_db: Database) -> None:
        assert Repository(memory_db).get_entities_by_ids([]) == []


# ── 1a/1c: embedder backends ─────────────────────────────────────────


class TestOllamaEmbedder:
    def test_embed_posts_to_api_embed(self) -> None:
        embedder = OllamaEmbedder(
            endpoint="http://localhost:11434", model="nomic-embed-text",
        )
        resp = MagicMock()
        resp.json.return_value = {"embeddings": [[1.0, 0.0], [0.0, 1.0]]}
        resp.raise_for_status.return_value = None

        with patch("httpx.post", return_value=resp) as post:
            out = embedder.embed(["a", "b"])

        assert out == [[1.0, 0.0], [0.0, 1.0]]
        url = post.call_args[0][0]
        assert url.endswith("/api/embed")
        assert post.call_args.kwargs["json"]["input"] == ["a", "b"]
        assert post.call_args.kwargs["json"]["model"] == "nomic-embed-text"

    def test_embed_returns_none_on_connect_error(self) -> None:
        import httpx

        embedder = OllamaEmbedder()
        with patch("httpx.post", side_effect=httpx.ConnectError("down")):
            assert embedder.embed(["a"]) is None

    def test_embed_returns_none_on_malformed_response(self) -> None:
        embedder = OllamaEmbedder()
        resp = MagicMock()
        resp.json.return_value = {"nope": 1}
        resp.raise_for_status.return_value = None
        with patch("httpx.post", return_value=resp):
            assert embedder.embed(["a"]) is None

    def test_embed_empty_input_returns_empty(self) -> None:
        with patch("httpx.post") as post:
            assert OllamaEmbedder().embed([]) == []
        post.assert_not_called()

    def test_is_available_checks_tags(self) -> None:
        embedder = OllamaEmbedder(model="nomic-embed-text")
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"models": [{"name": "nomic-embed-text:latest"}]}
        with patch("httpx.get", return_value=resp):
            assert embedder.is_available() is True

    def test_is_available_false_when_model_missing(self) -> None:
        embedder = OllamaEmbedder(model="nomic-embed-text")
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"models": [{"name": "qwen3:8b"}]}
        with patch("httpx.get", return_value=resp):
            assert embedder.is_available() is False


class TestOpenAICompatEmbedder:
    def test_embed_posts_to_embeddings(self) -> None:
        embedder = OpenAICompatEmbedder(
            endpoint="https://example.test/v1", model="text-embedding-3-small",
            api_key="k",
        )
        resp = MagicMock()
        resp.json.return_value = {
            "data": [
                {"index": 1, "embedding": [0.0, 1.0]},
                {"index": 0, "embedding": [1.0, 0.0]},
            ]
        }
        resp.raise_for_status.return_value = None

        with patch("httpx.post", return_value=resp) as post:
            out = embedder.embed(["a", "b"])

        # Provider may return data out of order — must be re-sorted by index.
        assert out == [[1.0, 0.0], [0.0, 1.0]]
        assert post.call_args[0][0].endswith("/embeddings")

    def test_embed_returns_none_without_api_key(self) -> None:
        embedder = OpenAICompatEmbedder(api_key="")
        with patch("httpx.post") as post:
            assert embedder.embed(["a"]) is None
        post.assert_not_called()

    def test_embed_returns_none_on_http_error(self) -> None:
        import httpx

        embedder = OpenAICompatEmbedder(api_key="k")
        request = httpx.Request("POST", "https://example.test/v1/embeddings")
        response = httpx.Response(403, request=request)
        with patch(
            "httpx.post",
            side_effect=httpx.HTTPStatusError(
                "forbidden", request=request, response=response,
            ),
        ):
            assert embedder.embed(["a"]) is None


class TestCreateEmbedder:
    def test_disabled_returns_none(self) -> None:
        config = Config(embeddings={"enabled": False})
        assert create_embedder(config) is None

    def test_backend_none_returns_none(self) -> None:
        config = Config(embeddings={"backend": "none"})
        assert create_embedder(config) is None

    def test_ollama_backend(self) -> None:
        config = Config(
            ollama={"endpoint": "http://box:11434"},
            embeddings={"backend": "ollama", "model": "nomic-embed-text"},
        )
        embedder = create_embedder(config)
        assert isinstance(embedder, OllamaEmbedder)
        assert embedder.endpoint == "http://box:11434"
        assert embedder.model == "nomic-embed-text"

    def test_embeddings_endpoint_overrides_backend_endpoint(self) -> None:
        config = Config(
            ollama={"endpoint": "http://box:11434"},
            embeddings={"backend": "ollama", "endpoint": "http://other:11434"},
        )
        assert create_embedder(config).endpoint == "http://other:11434"

    def test_openai_compat_backend_reads_key_env(self) -> None:
        config = Config(
            openai_compat={"api_key_env": "TEST_EMBED_KEY"},
            embeddings={"backend": "openai_compat", "model": "text-embedding-3-small"},
        )
        with patch.dict("os.environ", {"TEST_EMBED_KEY": "secret"}):
            embedder = create_embedder(config)
        assert isinstance(embedder, OpenAICompatEmbedder)
        assert embedder.api_key == "secret"

    def test_invalid_backend_rejected_by_config(self) -> None:
        with pytest.raises(ValueError, match="embeddings backend"):
            Config(embeddings={"backend": "magic"})


# ── 1c: embedding worker ─────────────────────────────────────────────


class TestEntityEmbedder:
    def test_enqueue_creates_job(self, memory_db: Database) -> None:
        project_id = _seed_project(memory_db)
        worker = EntityEmbedder(memory_db, _stub_config(), embedder=StubEmbedder({}))
        job_id = worker.enqueue(["a", "b"], project_id)

        queue = JobQueue(memory_db)
        job = queue.get_job(job_id)
        assert job is not None
        assert job.type == EMBED_JOB_TYPE
        assert job.payload["entity_ids"] == ["a", "b"]
        assert job.payload["project_id"] == project_id

    def test_process_job_writes_embeddings(self, memory_db: Database) -> None:
        project_id = _seed_project(memory_db)
        repo = Repository(memory_db)
        entity = Entity(
            project_id=project_id, type="decision",
            title="Adopt RRF", content="reciprocal rank fusion for search",
        )
        _insert_entity(memory_db, entity)

        stub = StubEmbedder({"rrf": [1.0, 0.0, 0.0]})
        worker = EntityEmbedder(memory_db, _stub_config(), embedder=stub)
        job_id = worker.enqueue([entity.id], project_id)
        queue = JobQueue(memory_db)
        job = queue.dequeue(EMBED_JOB_TYPE)
        assert job is not None and job.id == job_id

        assert worker.process_job(job) == 1

        row = repo.get_embedding(entity.id)
        assert row is not None
        assert row["model"] == STUB_KEY
        assert row["dim"] == 3
        assert unpack_vector(row["vector"]) == pytest.approx([1.0, 0.0, 0.0])

    def test_process_job_applies_document_prefix(
        self, memory_db: Database,
    ) -> None:
        project_id = _seed_project(memory_db)
        entity = Entity(
            project_id=project_id, type="fact", title="rrf", content="fusion",
        )
        _insert_entity(memory_db, entity)

        stub = StubEmbedder({"rrf": [1.0, 0.0, 0.0]})
        worker = EntityEmbedder(
            memory_db,
            _stub_config(document_prefix="search_document: "),
            embedder=stub,
        )
        worker.enqueue([entity.id], project_id)
        worker.process_job(JobQueue(memory_db).dequeue(EMBED_JOB_TYPE))

        assert stub.calls[0][0].startswith("search_document: ")

    def test_process_job_raises_when_backend_fails(
        self, memory_db: Database,
    ) -> None:
        """A backend outage must raise so the queue applies its backoff."""
        project_id = _seed_project(memory_db)
        entity = Entity(project_id=project_id, type="fact", title="t", content="c")
        _insert_entity(memory_db, entity)

        failing = StubEmbedder({})
        failing.embed = lambda texts, timeout=None: None  # type: ignore[assignment]
        worker = EntityEmbedder(memory_db, _stub_config(), embedder=failing)
        worker.enqueue([entity.id], project_id)
        job = JobQueue(memory_db).dequeue(EMBED_JOB_TYPE)

        with pytest.raises(RuntimeError, match="embedding backend"):
            worker.process_job(job)

    def test_process_job_without_embedder_is_a_noop(
        self, memory_db: Database,
    ) -> None:
        """No backend configured — the job completes, nothing is written."""
        project_id = _seed_project(memory_db)
        entity = Entity(project_id=project_id, type="fact", title="t", content="c")
        _insert_entity(memory_db, entity)

        worker = EntityEmbedder(
            memory_db, _stub_config(enabled=False), embedder=None,
        )
        worker.enqueue([entity.id], project_id)
        job = JobQueue(memory_db).dequeue(EMBED_JOB_TYPE)

        assert worker.process_job(job) == 0
        assert Repository(memory_db).get_embedding(entity.id) is None

    def test_process_job_skips_already_embedded(
        self, memory_db: Database,
    ) -> None:
        project_id = _seed_project(memory_db)
        repo = Repository(memory_db)
        entity = Entity(project_id=project_id, type="fact", title="rrf", content="c")
        _insert_entity(memory_db, entity)
        repo.upsert_embedding(
            entity.id, STUB_KEY, 3, pack_vector([1.0, 0.0, 0.0]),
        )

        stub = StubEmbedder({"rrf": [0.0, 1.0, 0.0]})
        worker = EntityEmbedder(memory_db, _stub_config(), embedder=stub)
        worker.enqueue([entity.id], project_id)
        job = JobQueue(memory_db).dequeue(EMBED_JOB_TYPE)

        assert worker.process_job(job) == 0
        assert stub.calls == []

    def test_process_pending_drains_queue(self, memory_db: Database) -> None:
        project_id = _seed_project(memory_db)
        stub = StubEmbedder({"t": [1.0, 0.0, 0.0]})
        worker = EntityEmbedder(memory_db, _stub_config(), embedder=stub)
        ids: list[str] = []
        for i in range(3):
            e = Entity(project_id=project_id, type="fact", title=f"t{i}", content="c")
            _insert_entity(memory_db, e)
            worker.enqueue([e.id], project_id)
            ids.append(e.id)

        assert worker.process_pending() == 3
        queue = JobQueue(memory_db)
        assert queue.get_pending_count(EMBED_JOB_TYPE) == 0
        repo = Repository(memory_db)
        assert all(repo.get_embedding(i) is not None for i in ids)

    def test_backfill_is_batched_and_resumable(self, memory_db: Database) -> None:
        project_id = _seed_project(memory_db)
        stub = StubEmbedder({"t": [1.0, 0.0, 0.0]})
        worker = EntityEmbedder(memory_db, _stub_config(), embedder=stub)
        for i in range(5):
            _insert_entity(memory_db, Entity(
                project_id=project_id, type="fact", title=f"t{i}", content="c",
            ))

        first = worker.backfill(project_id, batch_size=2, limit=2)
        assert first["embedded"] == 2
        assert first["remaining"] == 3
        # Batching: a limit of 2 with batch_size 2 is a single backend call.
        assert len(stub.calls) == 1

        second = worker.backfill(project_id, batch_size=2)
        assert second["embedded"] == 3
        assert second["remaining"] == 0

        # Resumable: re-running when nothing is missing is a no-op.
        third = worker.backfill(project_id, batch_size=2)
        assert third["embedded"] == 0

    def test_backfill_without_embedder_reports_disabled(
        self, memory_db: Database,
    ) -> None:
        project_id = _seed_project(memory_db)
        worker = EntityEmbedder(
            memory_db, _stub_config(enabled=False), embedder=None,
        )
        result = worker.backfill(project_id)
        assert result["embedded"] == 0
        assert result["disabled"] is True


class TestWorkerRunnerIntegration:
    def test_runner_dispatches_embed_job(self, memory_db: Database) -> None:
        from callmem.core.ollama import OllamaClient
        from callmem.core.workers import WorkerRunner

        project_id = _seed_project(memory_db)
        entity = Entity(
            project_id=project_id, type="fact", title="rrf", content="fusion",
        )
        _insert_entity(memory_db, entity)

        config = Config()
        runner = WorkerRunner(memory_db, OllamaClient(), config)
        stub = StubEmbedder({"rrf": [1.0, 0.0, 0.0]})
        runner._handlers[EMBED_JOB_TYPE].embedder = stub

        JobQueue(memory_db).enqueue(
            EMBED_JOB_TYPE, {"entity_ids": [entity.id], "project_id": project_id},
        )
        assert runner.process_one() is True

        queue = JobQueue(memory_db)
        assert queue.get_pending_count(EMBED_JOB_TYPE) == 0
        assert queue.get_status_summary()["failed"] == 0
        assert Repository(memory_db).get_embedding(entity.id) is not None

    def test_failed_embed_job_backs_off_then_resurrects(
        self, memory_db: Database,
    ) -> None:
        """Phase-0 contract: failure backs off, later success resurrects."""
        from callmem.core.ollama import OllamaClient
        from callmem.core.workers import WorkerRunner

        project_id = _seed_project(memory_db)
        entity = Entity(project_id=project_id, type="fact", title="t", content="c")
        _insert_entity(memory_db, entity)

        runner = WorkerRunner(memory_db, OllamaClient(), Config())
        failing = StubEmbedder({})
        failing.embed = lambda texts, timeout=None: None  # type: ignore[assignment]
        runner._handlers[EMBED_JOB_TYPE].embedder = failing

        queue = JobQueue(memory_db)
        job_id = queue.enqueue(
            EMBED_JOB_TYPE,
            {"entity_ids": [entity.id], "project_id": project_id},
            max_attempts=1,
        )
        runner.process_one()
        job = queue.get_job(job_id)
        assert job.status == "failed"

        # Backend recovers: a successful same-type job resurrects the dead one.
        runner._handlers[EMBED_JOB_TYPE].embedder = StubEmbedder({"t": [1.0, 0.0]})
        other = Entity(project_id=project_id, type="fact", title="t2", content="c")
        _insert_entity(memory_db, other)
        queue.enqueue(
            EMBED_JOB_TYPE, {"entity_ids": [other.id], "project_id": project_id},
        )
        runner.process_one()

        assert queue.get_job(job_id).status == "pending"

    def test_extraction_enqueues_embed_job(self, memory_db: Database) -> None:
        from callmem.core.extraction import EntityExtractor
        from callmem.core.ollama import OllamaClient
        from callmem.models.events import Event

        repo = Repository(memory_db)
        project = Project(name="p")
        repo.create_project(project)
        from callmem.models.sessions import Session

        session = Session(project_id=project.id)
        repo.insert_session(session)
        event = Event(
            session_id=session.id, project_id=project.id,
            type="note", content="we chose RRF",
        )
        repo.insert_event(event)

        ollama = OllamaClient()
        extractor = EntityExtractor(memory_db, ollama, config=_stub_config())
        job_id = extractor.enqueue_extraction([event.id], session.id)[0]
        job = JobQueue(memory_db).dequeue("extract_entities")
        assert job.id == job_id

        response = (
            '{"decisions": [{"title": "Use RRF", "content": "fuse rankings"}],'
            ' "todos": [], "facts": [], "failures": [], "discoveries": []}'
        )
        with patch.object(ollama, "_generate", return_value=response), _backend_up():
            entities = extractor.process_job(job)

        assert len(entities) == 1
        pending = JobQueue(memory_db).get_pending_count(EMBED_JOB_TYPE)
        assert pending == 1

    def test_extraction_does_not_enqueue_when_backend_unavailable(
        self, memory_db: Database,
    ) -> None:
        """Fleet safety: no embed jobs queued where no model is pulled."""
        from callmem.core.extraction import EntityExtractor
        from callmem.core.ollama import OllamaClient
        from callmem.models.events import Event
        from callmem.models.sessions import Session

        repo = Repository(memory_db)
        project = Project(name="p")
        repo.create_project(project)
        session = Session(project_id=project.id)
        repo.insert_session(session)
        event = Event(
            session_id=session.id, project_id=project.id,
            type="note", content="we chose RRF",
        )
        repo.insert_event(event)

        ollama = OllamaClient()
        extractor = EntityExtractor(memory_db, ollama, config=_stub_config())
        extractor.enqueue_extraction([event.id], session.id)
        job = JobQueue(memory_db).dequeue("extract_entities")

        response = (
            '{"decisions": [{"title": "Use RRF", "content": "fuse rankings"}],'
            ' "todos": [], "facts": [], "failures": [], "discoveries": []}'
        )
        with patch.object(ollama, "_generate", return_value=response), \
                _backend_down():
            entities = extractor.process_job(job)

        assert len(entities) == 1
        assert JobQueue(memory_db).get_pending_count(EMBED_JOB_TYPE) == 0

    def test_extraction_does_not_enqueue_when_disabled(
        self, memory_db: Database,
    ) -> None:
        from callmem.core.extraction import EntityExtractor
        from callmem.core.ollama import OllamaClient
        from callmem.models.events import Event
        from callmem.models.sessions import Session

        repo = Repository(memory_db)
        project = Project(name="p")
        repo.create_project(project)
        session = Session(project_id=project.id)
        repo.insert_session(session)
        event = Event(
            session_id=session.id, project_id=project.id,
            type="note", content="we chose RRF",
        )
        repo.insert_event(event)

        ollama = OllamaClient()
        extractor = EntityExtractor(
            memory_db, ollama, config=Config(embeddings={"enabled": False}),
        )
        job = JobQueue(memory_db).dequeue("extract_entities") or None
        extractor.enqueue_extraction([event.id], session.id)
        job = JobQueue(memory_db).dequeue("extract_entities")

        response = (
            '{"decisions": [{"title": "Use RRF", "content": "fuse rankings"}],'
            ' "todos": [], "facts": [], "failures": [], "discoveries": []}'
        )
        with patch.object(ollama, "_generate", return_value=response):
            extractor.process_job(job)

        assert JobQueue(memory_db).get_pending_count(EMBED_JOB_TYPE) == 0


# ── 1d: hybrid retrieval ─────────────────────────────────────────────


def _seed_hybrid(memory_db: Database) -> tuple[str, dict[str, str]]:
    """Seed three entities: one lexical match, one semantic-only, one noise."""
    project_id = _seed_project(memory_db)
    lexical = Entity(
        project_id=project_id, type="decision",
        title="pagination cursor", content="cursor pagination for list endpoints",
    )
    semantic = Entity(
        project_id=project_id, type="discovery",
        title="paging through results", content="offset windows over long result sets",
    )
    noise = Entity(
        project_id=project_id, type="fact",
        title="coffee machine", content="the office coffee machine is broken",
    )
    for e in (lexical, semantic, noise):
        _insert_entity(memory_db, e)
    return project_id, {
        "lexical": lexical.id, "semantic": semantic.id, "noise": noise.id,
    }


class TestHybridRetrieval:
    def test_fts_only_when_no_embeddings(self, memory_db: Database) -> None:
        from callmem.core.retrieval import RetrievalEngine

        project_id, ids = _seed_hybrid(memory_db)
        engine = RetrievalEngine(Repository(memory_db), _stub_config())
        results, mode = engine.search_with_mode(project_id, "pagination")

        assert mode == "fts"
        assert [r.id for r in results] == [ids["lexical"]]

    def test_graceful_degradation_is_byte_identical(
        self, memory_db: Database,
    ) -> None:
        """No embeddings stored -> results identical to embeddings-off config."""
        from callmem.core.retrieval import RetrievalEngine

        project_id, _ids = _seed_hybrid(memory_db)
        repo = Repository(memory_db)

        off = RetrievalEngine(repo, _stub_config(enabled=False))
        on = RetrievalEngine(repo, _stub_config(enabled=True))

        # Pin recency so the only thing that could differ between the two
        # runs is the code path itself, not the wall clock between calls.
        with patch("callmem.core.retrieval._recency_factor", return_value=0.5):
            baseline = off.search(project_id, "pagination cursor")
            degraded = on.search(project_id, "pagination cursor")

        assert [r.__dict__ for r in degraded] == [r.__dict__ for r in baseline]
        assert [r.score for r in degraded] == [r.score for r in baseline]

    def test_embeddings_backend_down_falls_back_to_fts(
        self, memory_db: Database,
    ) -> None:
        from callmem.core.retrieval import RetrievalEngine

        project_id, ids = _seed_hybrid(memory_db)
        repo = Repository(memory_db)
        # Embeddings exist, but the query embedding call fails.
        repo.upsert_embedding(
            ids["semantic"], STUB_KEY, 3, pack_vector([1.0, 0.0, 0.0]),
        )
        dead = StubEmbedder({})
        dead.embed = lambda texts, timeout=None: None  # type: ignore[assignment]

        engine = RetrievalEngine(repo, _stub_config(), embedder=dead)
        results, mode = engine.search_with_mode(project_id, "pagination")

        assert mode == "fts"
        assert [r.id for r in results] == [ids["lexical"]]

    def test_hybrid_surfaces_semantic_only_match(
        self, memory_db: Database,
    ) -> None:
        from callmem.core.retrieval import RetrievalEngine

        project_id, ids = _seed_hybrid(memory_db)
        repo = Repository(memory_db)
        repo.upsert_embedding(
            ids["lexical"], STUB_KEY, 3, pack_vector([0.9, 0.1, 0.0]),
        )
        repo.upsert_embedding(
            ids["semantic"], STUB_KEY, 3, pack_vector([1.0, 0.0, 0.0]),
        )
        repo.upsert_embedding(
            ids["noise"], STUB_KEY, 3, pack_vector([0.0, 0.0, 1.0]),
        )
        stub = StubEmbedder({"pagination": [1.0, 0.0, 0.0]})

        engine = RetrievalEngine(repo, _stub_config(), embedder=stub)
        results, mode = engine.search_with_mode(project_id, "pagination")

        assert mode == "hybrid"
        found = [r.id for r in results]
        # FTS alone would never return the semantic entity.
        assert ids["semantic"] in found
        assert ids["lexical"] in found
        # The unrelated entity stays below the similarity floor.
        assert ids["noise"] not in found

    def test_rrf_ranks_dual_list_hit_first(self, memory_db: Database) -> None:
        """An entity in both rankings outranks one in a single ranking."""
        from callmem.core.retrieval import RetrievalEngine

        project_id, ids = _seed_hybrid(memory_db)
        repo = Repository(memory_db)
        # lexical is an FTS hit AND the top vector hit -> must rank first.
        repo.upsert_embedding(
            ids["lexical"], STUB_KEY, 3, pack_vector([1.0, 0.0, 0.0]),
        )
        repo.upsert_embedding(
            ids["semantic"], STUB_KEY, 3, pack_vector([0.95, 0.05, 0.0]),
        )
        stub = StubEmbedder({"pagination": [1.0, 0.0, 0.0]})

        engine = RetrievalEngine(repo, _stub_config(), embedder=stub)
        results, mode = engine.search_with_mode(project_id, "pagination")

        assert mode == "hybrid"
        assert results[0].id == ids["lexical"]

    def test_query_prefix_is_applied(self, memory_db: Database) -> None:
        from callmem.core.retrieval import RetrievalEngine

        project_id, ids = _seed_hybrid(memory_db)
        repo = Repository(memory_db)
        repo.upsert_embedding(
            ids["semantic"], STUB_KEY, 3, pack_vector([1.0, 0.0, 0.0]),
        )
        stub = StubEmbedder({"pagination": [1.0, 0.0, 0.0]})

        engine = RetrievalEngine(
            repo, _stub_config(query_prefix="search_query: "),
            embedder=stub,
        )
        engine.search_with_mode(project_id, "pagination")

        assert stub.calls == [["search_query: pagination"]]

    def test_hybrid_respects_type_filter(self, memory_db: Database) -> None:
        from callmem.core.retrieval import RetrievalEngine

        project_id, ids = _seed_hybrid(memory_db)
        repo = Repository(memory_db)
        for key in ("lexical", "semantic", "noise"):
            repo.upsert_embedding(
                ids[key], STUB_KEY, 3, pack_vector([1.0, 0.0, 0.0]),
            )
        stub = StubEmbedder({"pagination": [1.0, 0.0, 0.0]})

        engine = RetrievalEngine(repo, _stub_config(), embedder=stub)
        results, _mode = engine.search_with_mode(
            project_id, "pagination", types=["discovery"],
        )
        assert all(r.type == "discovery" for r in results)

    def test_hybrid_ignores_dimension_mismatch(
        self, memory_db: Database,
    ) -> None:
        """A stored vector from a different model must not crash search."""
        from callmem.core.retrieval import RetrievalEngine

        project_id, ids = _seed_hybrid(memory_db)
        repo = Repository(memory_db)
        repo.upsert_embedding(
            ids["semantic"], STUB_KEY, 5,
            pack_vector([1.0, 0.0, 0.0, 0.0, 0.0]),
        )
        stub = StubEmbedder({"pagination": [1.0, 0.0, 0.0]})

        engine = RetrievalEngine(repo, _stub_config(), embedder=stub)
        results, _mode = engine.search_with_mode(project_id, "pagination")
        assert [r.id for r in results] == [ids["lexical"]]

    def test_browse_without_query_stays_fts(self, memory_db: Database) -> None:
        from callmem.core.retrieval import RetrievalEngine

        project_id, ids = _seed_hybrid(memory_db)
        repo = Repository(memory_db)
        repo.upsert_embedding(
            ids["semantic"], STUB_KEY, 3, pack_vector([1.0, 0.0, 0.0]),
        )
        stub = StubEmbedder({"pagination": [1.0, 0.0, 0.0]})

        engine = RetrievalEngine(repo, _stub_config(), embedder=stub)
        _results, mode = engine.search_with_mode(project_id, "")
        assert mode == "fts"


class TestSearchMode:
    def test_mem_search_reports_fts_mode(self, memory_db: Database) -> None:
        from callmem.core.engine import MemoryEngine
        from callmem.mcp.tools import handle_search

        engine = MemoryEngine(memory_db, Config())
        engine.start_session()
        engine.ingest_one("note", "cursor pagination for list endpoints")

        payload = _tool_payload(handle_search(engine, {"query": "pagination"}))
        assert payload["mode"] == "fts"

    def test_mem_search_reports_hybrid_mode(self, memory_db: Database) -> None:
        from callmem.core.engine import MemoryEngine
        from callmem.mcp.tools import handle_search

        engine = MemoryEngine(memory_db, _stub_config())
        entity = Entity(
            project_id=engine.project_id, type="decision",
            title="pagination cursor", content="cursor pagination",
        )
        _insert_entity(memory_db, entity)
        engine.repo.upsert_embedding(
            entity.id, STUB_KEY, 3, pack_vector([1.0, 0.0, 0.0]),
        )

        stub = StubEmbedder({"pagination": [1.0, 0.0, 0.0]})
        with patch(
            "callmem.core.retrieval.create_embedder", return_value=stub,
        ):
            payload = _tool_payload(handle_search(engine, {"query": "pagination"}))

        assert payload["mode"] == "hybrid"
        assert payload["results"]


# ── Fix round 1 ──────────────────────────────────────────────────────


class TestModelKeyInvalidation:
    """Fix 3: document_prefix is part of the stored vector's identity."""

    def test_key_folds_model_and_document_prefix(self) -> None:
        from callmem.core.embeddings import embedding_model_key

        config = Config(embeddings={
            "model": "m", "document_prefix": "doc: ", "query_prefix": "q: ",
        })
        assert embedding_model_key(config) == "m|doc: "

    def test_query_prefix_does_not_change_key(self) -> None:
        from callmem.core.embeddings import embedding_model_key

        base = {"model": "m", "document_prefix": "doc: "}
        a = embedding_model_key(Config(embeddings={**base, "query_prefix": "x"}))
        b = embedding_model_key(Config(embeddings={**base, "query_prefix": "y"}))
        assert a == b

    def test_prefix_change_makes_entities_backfillable(
        self, memory_db: Database,
    ) -> None:
        project_id = _seed_project(memory_db)
        repo = Repository(memory_db)
        entity = Entity(project_id=project_id, type="fact", title="t", content="c")
        _insert_entity(memory_db, entity)

        old = EntityEmbedder(
            memory_db, _stub_config(document_prefix="old: "),
            embedder=StubEmbedder({"t": [1.0, 0.0, 0.0]}),
        )
        assert old.backfill(project_id)["embedded"] == 1
        assert repo.count_entities_missing_embeddings(project_id, old.model_key) == 0

        new = EntityEmbedder(
            memory_db, _stub_config(document_prefix="new: "),
            embedder=StubEmbedder({"t": [0.0, 1.0, 0.0]}),
        )
        assert new.model_key != old.model_key
        assert repo.count_entities_missing_embeddings(project_id, new.model_key) == 1
        assert new.backfill(project_id)["embedded"] == 1

    def test_stale_prefix_vectors_are_not_searched(
        self, memory_db: Database,
    ) -> None:
        from callmem.core.retrieval import RetrievalEngine

        project_id, ids = _seed_hybrid(memory_db)
        repo = Repository(memory_db)
        # Vector stored under a prefix the engine is no longer configured for.
        repo.upsert_embedding(
            ids["semantic"], "stub-embed|old: ", 3, pack_vector([1.0, 0.0, 0.0]),
        )
        engine = RetrievalEngine(
            repo, _stub_config(document_prefix="new: "),
            embedder=StubEmbedder({"pagination": [1.0, 0.0, 0.0]}),
        )
        results, mode = engine.search_with_mode(project_id, "pagination")

        assert mode == "fts"
        assert [r.id for r in results] == [ids["lexical"]]

    def test_zero_candidates_is_logged_once(
        self, memory_db: Database, caplog: Any,
    ) -> None:
        from callmem.core.retrieval import RetrievalEngine

        project_id, ids = _seed_hybrid(memory_db)
        repo = Repository(memory_db)
        repo.upsert_embedding(
            ids["semantic"], "stub-embed|old: ", 3, pack_vector([1.0, 0.0, 0.0]),
        )
        engine = RetrievalEngine(
            repo, _stub_config(document_prefix="new: "),
            embedder=StubEmbedder({"pagination": [1.0, 0.0, 0.0]}),
        )
        with caplog.at_level("WARNING"):
            engine.search_with_mode(project_id, "pagination")
            engine.search_with_mode(project_id, "pagination")

        hits = [r for r in caplog.records if "none for the active key" in r.message]
        assert len(hits) == 1


class TestQueryTimeout:
    """Fix 5: the search path must not inherit the 60s ingest budget."""

    def test_query_uses_query_timeout(self, memory_db: Database) -> None:
        from callmem.core.retrieval import RetrievalEngine

        project_id, ids = _seed_hybrid(memory_db)
        repo = Repository(memory_db)
        repo.upsert_embedding(
            ids["semantic"], STUB_KEY, 3, pack_vector([1.0, 0.0, 0.0]),
        )
        stub = StubEmbedder({"pagination": [1.0, 0.0, 0.0]})
        engine = RetrievalEngine(
            repo, _stub_config(timeout=60, query_timeout=2.5), embedder=stub,
        )
        engine.search_with_mode(project_id, "pagination")

        assert stub.timeouts == [2.5]

    def test_ingest_path_keeps_full_timeout(self, memory_db: Database) -> None:
        project_id = _seed_project(memory_db)
        entity = Entity(project_id=project_id, type="fact", title="t", content="c")
        _insert_entity(memory_db, entity)

        stub = StubEmbedder({"t": [1.0, 0.0, 0.0]})
        worker = EntityEmbedder(
            memory_db, _stub_config(timeout=60, query_timeout=2.5), embedder=stub,
        )
        worker.enqueue([entity.id], project_id)
        worker.process_job(JobQueue(memory_db).dequeue(EMBED_JOB_TYPE))

        # None = "use the backend's own (ingest) timeout".
        assert stub.timeouts == [None]

    def test_timeout_falls_back_to_fts_and_logs_once(
        self, memory_db: Database, caplog: Any,
    ) -> None:
        import httpx

        from callmem.core.retrieval import RetrievalEngine

        project_id, ids = _seed_hybrid(memory_db)
        repo = Repository(memory_db)
        repo.upsert_embedding(
            ids["semantic"], STUB_KEY, 3, pack_vector([1.0, 0.0, 0.0]),
        )
        hung = OllamaEmbedder(model=STUB_MODEL)
        engine = RetrievalEngine(repo, _stub_config(), embedder=hung)

        with caplog.at_level("WARNING"), patch(
            "httpx.post", side_effect=httpx.ReadTimeout("hung"),
        ):
            first, mode = engine.search_with_mode(project_id, "pagination")
            engine.search_with_mode(project_id, "pagination")

        assert mode == "fts"
        assert [r.id for r in first] == [ids["lexical"]]
        hits = [r for r in caplog.records if "timed out" in r.message]
        assert len(hits) == 1

    def test_ollama_embed_passes_timeout_through(self) -> None:
        embedder = OllamaEmbedder()
        resp = MagicMock()
        resp.json.return_value = {"embeddings": [[1.0, 0.0]]}
        resp.raise_for_status.return_value = None
        with patch("httpx.post", return_value=resp) as post:
            embedder.embed(["a"], timeout=1.5)
        assert post.call_args.kwargs["timeout"] == 1.5

    def test_openai_compat_embed_passes_timeout_through(self) -> None:
        embedder = OpenAICompatEmbedder(api_key="k")
        resp = MagicMock()
        resp.json.return_value = {"data": [{"index": 0, "embedding": [1.0, 0.0]}]}
        resp.raise_for_status.return_value = None
        with patch("httpx.post", return_value=resp) as post:
            embedder.embed(["a"], timeout=1.5)
        assert post.call_args.kwargs["timeout"] == 1.5


class TestEmptyVectorRejection:
    """Fix 2: an empty inner vector must be a backend failure, not a skip."""

    def test_ollama_rejects_empty_vector(self) -> None:
        resp = MagicMock()
        resp.json.return_value = {"embeddings": [[]]}
        resp.raise_for_status.return_value = None
        with patch("httpx.post", return_value=resp):
            assert OllamaEmbedder().embed(["a"]) is None

    def test_ollama_rejects_mixed_dimensions(self) -> None:
        resp = MagicMock()
        resp.json.return_value = {"embeddings": [[1.0, 0.0], [1.0, 0.0, 0.0]]}
        resp.raise_for_status.return_value = None
        with patch("httpx.post", return_value=resp):
            assert OllamaEmbedder().embed(["a", "b"]) is None

    def test_ollama_rejects_non_numeric_vector(self) -> None:
        resp = MagicMock()
        resp.json.return_value = {"embeddings": [["oops"]]}
        resp.raise_for_status.return_value = None
        with patch("httpx.post", return_value=resp):
            assert OllamaEmbedder().embed(["a"]) is None

    def test_openai_compat_rejects_empty_vector(self) -> None:
        resp = MagicMock()
        resp.json.return_value = {"data": [{"index": 0, "embedding": []}]}
        resp.raise_for_status.return_value = None
        with patch("httpx.post", return_value=resp):
            assert OpenAICompatEmbedder(api_key="k").embed(["a"]) is None

    def test_backfill_stops_instead_of_looping_forever(
        self, memory_db: Database,
    ) -> None:
        """The regression this fix exists for: a 0-write pass must not repeat."""
        project_id = _seed_project(memory_db)
        for i in range(3):
            _insert_entity(memory_db, Entity(
                project_id=project_id, type="fact", title=f"t{i}", content="c",
            ))

        stub = StubEmbedder({"t": [1.0, 0.0, 0.0]})
        # A backend that returns the right count of empty vectors: writes
        # nothing, leaves every row still "missing".
        stub.embed = (  # type: ignore[assignment]
            lambda texts, timeout=None: [[] for _ in texts]
        )
        worker = EntityEmbedder(memory_db, _stub_config(), embedder=stub)

        result = worker.backfill(project_id, batch_size=1)

        assert result["embedded"] == 0
        assert result["stalled"] is True
        assert result["remaining"] == 3


class TestAvailabilityGate:
    """Fix 7: don't queue embed jobs against a backend that isn't there."""

    def test_enqueue_skipped_when_unavailable(self, memory_db: Database) -> None:
        from callmem.core.embeddings import enqueue_embeddings

        project_id = _seed_project(memory_db)
        with _backend_down():
            job_id = enqueue_embeddings(
                JobQueue(memory_db), _stub_config(), ["e1"], project_id,
            )
        assert job_id is None
        assert JobQueue(memory_db).get_pending_count(EMBED_JOB_TYPE) == 0

    def test_enqueue_proceeds_when_available(self, memory_db: Database) -> None:
        from callmem.core.embeddings import enqueue_embeddings

        project_id = _seed_project(memory_db)
        with _backend_up():
            job_id = enqueue_embeddings(
                JobQueue(memory_db), _stub_config(), ["e1"], project_id,
            )
        assert job_id is not None
        assert JobQueue(memory_db).get_pending_count(EMBED_JOB_TYPE) == 1

    def test_enqueue_skipped_when_disabled(self, memory_db: Database) -> None:
        from callmem.core.embeddings import enqueue_embeddings

        project_id = _seed_project(memory_db)
        with _backend_up():
            job_id = enqueue_embeddings(
                JobQueue(memory_db), _stub_config(enabled=False),
                ["e1"], project_id,
            )
        assert job_id is None

    def test_probe_is_cached_across_calls(self) -> None:
        from callmem.core.embeddings import embedding_backend_available

        config = _stub_config()
        with patch.object(
            OllamaEmbedder, "is_available", return_value=False,
        ) as probe:
            assert embedding_backend_available(config) is False
            assert embedding_backend_available(config) is False
        assert probe.call_count == 1

    def test_negative_probe_rechecked_after_interval(self) -> None:
        from callmem.core import embeddings as emb

        config = _stub_config()
        with patch.object(
            OllamaEmbedder, "is_available", return_value=False,
        ) as probe:
            assert emb.embedding_backend_available(config) is False
            # Age the cached negative past its recheck window.
            key = next(iter(emb._availability))
            available, checked_at = emb._availability[key]
            emb._availability[key] = (
                available, checked_at - emb.AVAILABILITY_RECHECK_SECONDS - 1,
            )
            assert emb.embedding_backend_available(config) is False
        assert probe.call_count == 2

    def test_unavailable_backend_logs_loudly(self, caplog: Any) -> None:
        from callmem.core.embeddings import embedding_backend_available

        with caplog.at_level("WARNING"), patch.object(
            OllamaEmbedder, "is_available", return_value=False,
        ):
            embedding_backend_available(_stub_config())

        assert any(
            "Embedding backend unavailable" in r.message for r in caplog.records
        )


class TestDirectEntityCreationCoverage:
    """Fix 1: every entity-creation path must reach the embedding queue."""

    def test_typed_event_entity_is_enqueued(self, memory_db: Database) -> None:
        from callmem.core.engine import MemoryEngine

        engine = MemoryEngine(memory_db, _stub_config())
        engine.start_session()
        with _backend_up():
            engine.ingest_one("decision", "We will use cursor pagination")

        entities = engine.repo.get_entities(engine.project_id)
        assert len(entities) == 1

        queue = JobQueue(memory_db)
        assert queue.get_pending_count(EMBED_JOB_TYPE) == 1
        job = queue.dequeue(EMBED_JOB_TYPE)
        assert job.payload["entity_ids"] == [entities[0]["id"]]
        assert job.payload["project_id"] == engine.project_id

    def test_typed_event_respects_availability_gate(
        self, memory_db: Database,
    ) -> None:
        from callmem.core.engine import MemoryEngine

        engine = MemoryEngine(memory_db, _stub_config())
        engine.start_session()
        with _backend_down():
            engine.ingest_one("decision", "We will use cursor pagination")

        assert len(engine.repo.get_entities(engine.project_id)) == 1
        assert JobQueue(memory_db).get_pending_count(EMBED_JOB_TYPE) == 0

    def test_reextraction_enqueues_embeddings(self, memory_db: Database) -> None:
        from callmem.core.ollama import OllamaClient
        from callmem.core.reextraction import ReExtractor
        from callmem.models.events import Event
        from callmem.models.sessions import Session

        repo = Repository(memory_db)
        project = Project(name="p")
        repo.create_project(project)
        session = Session(project_id=project.id)
        repo.insert_session(session)
        event = Event(
            session_id=session.id, project_id=project.id,
            type="note", content="we chose RRF for fusion",
        )
        repo.insert_event(event)

        ollama = OllamaClient()
        reex = ReExtractor(memory_db, ollama, _stub_config())
        response = (
            '{"decisions": [{"title": "Use RRF", "content": "fuse rankings"}],'
            ' "todos": [], "facts": [], "failures": [], "discoveries": []}'
        )
        with patch.object(ollama, "_generate", return_value=response), _backend_up():
            result = reex.run(project.id)

        assert result["entities_created"] == 1
        queue = JobQueue(memory_db)
        assert queue.get_pending_count(EMBED_JOB_TYPE) == 1
        job = queue.dequeue(EMBED_JOB_TYPE)
        assert job.payload["project_id"] == project.id
        assert len(job.payload["entity_ids"]) == 1

    def test_reextraction_respects_availability_gate(
        self, memory_db: Database,
    ) -> None:
        from callmem.core.ollama import OllamaClient
        from callmem.core.reextraction import ReExtractor
        from callmem.models.events import Event
        from callmem.models.sessions import Session

        repo = Repository(memory_db)
        project = Project(name="p")
        repo.create_project(project)
        session = Session(project_id=project.id)
        repo.insert_session(session)
        repo.insert_event(Event(
            session_id=session.id, project_id=project.id,
            type="note", content="we chose RRF for fusion",
        ))

        ollama = OllamaClient()
        reex = ReExtractor(memory_db, ollama, _stub_config())
        response = (
            '{"decisions": [{"title": "Use RRF", "content": "fuse rankings"}],'
            ' "todos": [], "facts": [], "failures": [], "discoveries": []}'
        )
        with patch.object(ollama, "_generate", return_value=response), \
                _backend_down():
            reex.run(project.id)

        assert JobQueue(memory_db).get_pending_count(EMBED_JOB_TYPE) == 0


class TestMissingEmbeddingCount:
    """Fix 6: counting must not materialise rows or cap at a LIMIT."""

    def test_count_matches_reality_past_10k(self, memory_db: Database) -> None:
        project_id = _seed_project(memory_db)
        repo = Repository(memory_db)
        conn = memory_db.connect()
        try:
            rows = []
            for i in range(10_500):
                e = Entity(
                    project_id=project_id, type="fact",
                    title=f"t{i}", content="c",
                )
                r = e.to_row()
                rows.append((
                    r["id"], r["project_id"], r["type"], r["title"],
                    r["content"], r["created_at"], r["updated_at"],
                ))
            conn.executemany(
                "INSERT INTO entities (id, project_id, type, title, content, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            conn.commit()
        finally:
            conn.close()

        assert repo.count_entities_missing_embeddings(project_id, STUB_KEY) == 10_500

    def test_count_excludes_archived_and_embedded(
        self, memory_db: Database,
    ) -> None:
        project_id = _seed_project(memory_db)
        repo = Repository(memory_db)
        live = Entity(project_id=project_id, type="fact", title="a", content="c")
        done = Entity(project_id=project_id, type="fact", title="b", content="c")
        arch = Entity(project_id=project_id, type="fact", title="c", content="c")
        for e in (live, done, arch):
            _insert_entity(memory_db, e)
        repo.upsert_embedding(done.id, STUB_KEY, 3, pack_vector([1.0, 0.0, 0.0]))
        conn = memory_db.connect()
        try:
            conn.execute(
                "UPDATE entities SET archived_at = datetime('now') WHERE id = ?",
                (arch.id,),
            )
            conn.commit()
        finally:
            conn.close()

        assert repo.count_entities_missing_embeddings(project_id, STUB_KEY) == 1


def _tool_payload(content: Any) -> dict[str, Any]:
    import json

    return json.loads(content[0].text)


# ── 1b: performance ──────────────────────────────────────────────────


class TestVectorSearchPerformance:
    def test_under_200ms_for_10k_entities(self, tmp_path: Path) -> None:
        """Prefiltered cosine must stay under 200ms at 10k entities."""
        import random

        from callmem.core.retrieval import RetrievalEngine

        db = Database(tmp_path / "perf.db")
        db.initialize()
        project_id = _seed_project(db)
        repo = Repository(db)

        random.seed(7)
        dim = 768
        conn = db.connect()
        try:
            entity_rows = []
            embed_rows = []
            for i in range(10_000):
                e = Entity(
                    project_id=project_id, type="fact",
                    title=f"entity {i} pagination", content=f"body {i}",
                )
                row = e.to_row()
                entity_rows.append((
                    row["id"], row["project_id"], row["type"], row["title"],
                    row["content"], row["created_at"], row["updated_at"],
                ))
                vec = [random.gauss(0, 1) for _ in range(dim)]
                norm = math.sqrt(sum(x * x for x in vec)) or 1.0
                embed_rows.append((
                    row["id"], "perf-model|", dim,
                    pack_vector([x / norm for x in vec]),
                ))
            conn.executemany(
                "INSERT INTO entities (id, project_id, type, title, content, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                entity_rows,
            )
            conn.executemany(
                "INSERT INTO embeddings (entity_id, model, dim, vector, created_at) "
                "VALUES (?, ?, ?, ?, datetime('now'))",
                embed_rows,
            )
            conn.commit()
        finally:
            conn.close()

        assert repo.count_embeddings(project_id) == 10_000

        query_vec = [random.gauss(0, 1) for _ in range(dim)]
        norm = math.sqrt(sum(x * x for x in query_vec)) or 1.0
        query_vec = [x / norm for x in query_vec]

        class _PerfEmbedder:
            model = "perf-model"
            dim = 768

            def is_available(self) -> bool:
                return True

            def embed(
                self, texts: list[str], timeout: float | None = None,
            ) -> list[list[float]]:
                return [list(query_vec) for _ in texts]

        config = _stub_config(model="perf-model", min_similarity=-1.0)
        engine = RetrievalEngine(repo, config, embedder=_PerfEmbedder())

        start = time.perf_counter()
        ranked = engine._vector_ranked_ids(
            project_id, "pagination", types=None, include_stale=False, limit=20,
        )
        elapsed_ms = (time.perf_counter() - start) * 1000

        assert ranked
        assert elapsed_ms < 200, f"vector search took {elapsed_ms:.1f}ms"


# ── 1c: CLI ──────────────────────────────────────────────────────────


class TestEmbedCli:
    def test_embed_status_reports_counts(self, tmp_path: Path) -> None:
        from click.testing import CliRunner

        from callmem.cli import main

        runner = CliRunner()
        assert runner.invoke(main, ["init", "--project", str(tmp_path)]).exit_code == 0

        result = runner.invoke(main, ["embed", "--project", str(tmp_path)])
        assert result.exit_code == 0
        assert "embedded" in result.output.lower()

    def test_embed_missing_database(self, tmp_path: Path) -> None:
        from click.testing import CliRunner

        from callmem.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["embed", "--project", str(tmp_path)])
        assert result.exit_code == 0
        assert "No callmem database" in result.output

    def test_backfill_is_non_interactive_with_yes(self, tmp_path: Path) -> None:
        from click.testing import CliRunner

        from callmem.cli import main
        runner = CliRunner()
        assert runner.invoke(main, ["init", "--project", str(tmp_path)]).exit_code == 0

        from callmem.core.config import load_config
        from callmem.core.engine import MemoryEngine

        db = Database(tmp_path / ".callmem" / "memory.db")
        db.initialize()
        repo = Repository(db)
        project_id = MemoryEngine(db, load_config(tmp_path)).project_id
        entity = Entity(
            project_id=project_id, type="fact", title="rrf", content="fusion",
        )
        _insert_entity(db, entity)

        stub = StubEmbedder({"rrf": [1.0, 0.0, 0.0]})
        with patch("callmem.core.embeddings.create_embedder", return_value=stub):
            result = runner.invoke(
                main,
                ["embed", "--project", str(tmp_path), "--backfill", "--yes"],
                input="",
            )

        assert result.exit_code == 0, result.output
        assert repo.get_embedding(entity.id) is not None

    def test_backfill_without_backend_degrades(self, tmp_path: Path) -> None:
        from click.testing import CliRunner

        from callmem.cli import main

        runner = CliRunner()
        assert runner.invoke(main, ["init", "--project", str(tmp_path)]).exit_code == 0

        with patch("callmem.core.embeddings.create_embedder", return_value=None):
            result = runner.invoke(
                main, ["embed", "--project", str(tmp_path), "--backfill", "--yes"],
            )

        assert result.exit_code == 0
        assert "disabled" in result.output.lower()
