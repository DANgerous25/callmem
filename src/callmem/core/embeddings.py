"""Entity embeddings: backends, storage helpers, and the embedding worker.

Vectors are stored as packed float32 blobs in the `embeddings` table and
scored in Python over a prefiltered candidate set (see
``retrieval.RetrievalEngine``). No SQLite extension is required, so a
callmem database upgrades and opens identically whether or not any
vector tooling is installed.

Everything here degrades to a no-op when no embedding backend is
reachable: search then serves pure-FTS results exactly as before.
"""

from __future__ import annotations

import logging
import math
import os
import struct
import time
from abc import ABC, abstractmethod
from operator import mul
from typing import TYPE_CHECKING, Any

import httpx

from callmem.core.queue import JobQueue

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from callmem.core.database import Database
    from callmem.models.config import Config

logger = logging.getLogger(__name__)

EMBED_JOB_TYPE = "embed_entities"

#: Cap on the text handed to the embedding model per entity. Embedding
#: models truncate anyway; doing it here keeps request bodies bounded.
MAX_EMBED_CHARS = 2000

#: How long a negative availability probe is trusted before re-checking.
#: Bounds the damage of enabled-by-default config on a machine that never
#: pulled an embedding model, without permanently giving up on a backend
#: that comes up later.
AVAILABILITY_RECHECK_SECONDS = 3600.0


# ── Vector helpers ───────────────────────────────────────────────────


def pack_vector(values: Sequence[float]) -> bytes:
    """Pack a float sequence into a little-endian float32 blob."""
    return struct.pack(f"<{len(values)}f", *values)


def unpack_vector(blob: bytes) -> tuple[float, ...]:
    """Unpack a float32 blob written by :func:`pack_vector`."""
    return struct.unpack(f"<{len(blob) // 4}f", blob)


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity, or 0.0 for mismatched/degenerate inputs.

    Returning 0.0 rather than raising means a vector left behind by a
    different embedding model can never break a search.
    """
    if len(a) != len(b):
        return 0.0
    dot = float(sum(map(mul, a, b)))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def rank_by_similarity(
    query_vector: Sequence[float],
    candidates: Sequence[Mapping[str, Any]],
    min_similarity: float,
) -> list[tuple[float, str]]:
    """Score packed candidate vectors against a query vector, best first.

    Candidates are ``{entity_id, dim, vector}`` rows as returned by
    ``Repository.load_embedding_candidates``. Rows whose ``dim`` differs
    from the query (a leftover from another embedding model) are skipped
    without unpacking, and anything below ``min_similarity`` is dropped.

    This is the hot path of every hybrid search, so the query's norm is
    hoisted out of the loop: the inner loop does one dot product and one
    candidate-norm pass rather than recomputing all three per candidate.
    """
    dim = len(query_vector)
    query_norm = math.sqrt(sum(map(mul, query_vector, query_vector)))
    if dim == 0 or query_norm == 0.0:
        return []

    scored: list[tuple[float, str]] = []
    for candidate in candidates:
        if candidate["dim"] != dim:
            continue
        vector = unpack_vector(candidate["vector"])
        norm = math.sqrt(sum(map(mul, vector, vector)))
        if norm == 0.0:
            continue
        similarity = sum(map(mul, query_vector, vector)) / (query_norm * norm)
        if similarity >= min_similarity:
            scored.append((similarity, str(candidate["entity_id"])))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    return scored


def entity_embedding_text(
    row: Mapping[str, Any], max_chars: int = MAX_EMBED_CHARS,
) -> str:
    """Build the text used to embed an entity.

    Title first (it carries the most signal per token), then synopsis and
    key points, then the body — so truncation drops detail, not identity.
    """
    parts = [
        str(row.get(field) or "").strip()
        for field in ("title", "synopsis", "key_points", "content")
    ]
    return "\n".join(p for p in parts if p)[:max_chars]


# ── Backends ─────────────────────────────────────────────────────────


def _validate_vectors(
    raw: Any, expected: int, backend: str,
) -> list[list[float]] | None:
    """Coerce a backend payload into vectors, or None if it is unusable.

    Rejects the shapes that would otherwise poison storage silently: the
    wrong number of vectors, an empty inner vector, or vectors of
    inconsistent dimensionality. An empty vector is especially dangerous —
    it writes a zero-length blob that no query can ever match, and (before
    this check) let the backfill loop re-fetch the same rows forever.
    """
    if not isinstance(raw, list) or len(raw) != expected:
        logger.warning(
            "%s embed returned %s vector(s) for %d input(s)",
            backend, len(raw) if isinstance(raw, list) else "no", expected,
        )
        return None

    vectors: list[list[float]] = []
    for item in raw:
        if not isinstance(item, list) or not item:
            logger.warning("%s embed returned an empty vector", backend)
            return None
        try:
            vectors.append([float(x) for x in item])
        except (TypeError, ValueError):
            logger.warning("%s embed returned a non-numeric vector", backend)
            return None

    dims = {len(v) for v in vectors}
    if len(dims) != 1:
        logger.warning(
            "%s embed returned mixed dimensions %s", backend, sorted(dims),
        )
        return None
    return vectors


class Embedder(ABC):
    """Minimal embedding backend interface.

    ``embed`` returns one vector per input text, or ``None`` when the
    backend is unreachable or answered with something unusable — callers
    treat ``None`` as "try again later", never as "no results".
    """

    model: str

    @abstractmethod
    def embed(
        self, texts: list[str], timeout: float | None = None,
    ) -> list[list[float]] | None:
        """Embed a batch of texts, or return None on backend failure.

        ``timeout`` overrides the backend's configured timeout for this
        call only — the interactive search path uses a much tighter
        budget than ingest/backfill, which can afford to wait.
        """

    @abstractmethod
    def is_available(self) -> bool:
        """Whether the backend is reachable and serving ``self.model``."""


class OllamaEmbedder(Embedder):
    """Local Ollama ``/api/embed`` backend (preferred: free and private)."""

    def __init__(
        self,
        endpoint: str = "http://localhost:11434",
        model: str = "nomic-embed-text",
        timeout: int = 60,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.model = model
        self.timeout = timeout

    def is_available(self) -> bool:
        try:
            resp = httpx.get(f"{self.endpoint}/api/tags", timeout=5.0)
            if resp.status_code != 200:
                return False
            names = {
                str(m.get("name", "")) for m in resp.json().get("models", [])
            }
        except (httpx.HTTPError, ValueError):
            return False
        # Ollama reports "nomic-embed-text:latest" for an untagged pull.
        return any(
            n == self.model or n.split(":", 1)[0] == self.model.split(":", 1)[0]
            for n in names
        )

    def embed(
        self, texts: list[str], timeout: float | None = None,
    ) -> list[list[float]] | None:
        if not texts:
            return []
        try:
            resp = httpx.post(
                f"{self.endpoint}/api/embed",
                json={"model": self.model, "input": texts},
                timeout=self.timeout if timeout is None else timeout,
            )
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("Ollama embed failed: %s", exc)
            return None

        return _validate_vectors(data.get("embeddings"), len(texts), "Ollama")


class OpenAICompatEmbedder(Embedder):
    """Any OpenAI-compatible ``/embeddings`` endpoint."""

    def __init__(
        self,
        endpoint: str = "https://openrouter.ai/api/v1",
        model: str = "text-embedding-3-small",
        api_key: str | None = None,
        timeout: int = 60,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.model = model
        self.api_key = api_key or ""
        self.timeout = timeout

    def is_available(self) -> bool:
        return bool(self.api_key) and self.embed(["ping"]) is not None

    def embed(
        self, texts: list[str], timeout: float | None = None,
    ) -> list[list[float]] | None:
        if not texts:
            return []
        if not self.api_key:
            logger.warning("OpenAI-compat embed skipped: no API key configured")
            return None
        try:
            resp = httpx.post(
                f"{self.endpoint}/embeddings",
                json={"model": self.model, "input": texts},
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.api_key}",
                },
                timeout=self.timeout if timeout is None else timeout,
            )
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("OpenAI-compat embed failed: %s", exc)
            return None

        items = data.get("data")
        if not isinstance(items, list):
            logger.warning("OpenAI-compat embed returned an unusable payload")
            return None
        # The spec allows results in any order; `index` is authoritative.
        try:
            ordered = sorted(items, key=lambda d: int(d.get("index", 0)))
            raw = [d["embedding"] for d in ordered]
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("OpenAI-compat embed payload malformed: %s", exc)
            return None

        return _validate_vectors(raw, len(texts), "OpenAI-compat")


def embedding_model_key(config: Config) -> str:
    """The identity a stored vector is keyed by, written to `embeddings.model`.

    A stored vector is fully determined by the model *and* the document
    prefix that was prepended before embedding, so both belong in the key.
    Changing either makes every existing vector a non-match for
    ``list_entities_missing_embeddings`` and ``load_embedding_candidates``,
    which auto-invalidates the old corpus: stale-prefix vectors stop being
    searched and their entities become backfill candidates again.

    ``query_prefix`` is deliberately *not* part of the key — it is applied
    fresh to every query, so changing it cannot leave stale data behind.
    """
    settings = config.embeddings
    return f"{settings.model}|{settings.document_prefix}"


def create_embedder(config: Config) -> Embedder | None:
    """Build the configured embedder, or None when the feature is off.

    Returning None is the dormant state: callers must keep working
    without embeddings rather than treating it as an error.
    """
    settings = config.embeddings
    if not settings.enabled or settings.backend == "none":
        logger.info(
            "Embeddings disabled (enabled=%s, backend=%s) — "
            "search will use FTS only",
            settings.enabled, settings.backend,
        )
        return None

    if settings.backend == "ollama":
        return OllamaEmbedder(
            endpoint=settings.endpoint or config.ollama.endpoint,
            model=settings.model,
            timeout=settings.timeout,
        )

    return OpenAICompatEmbedder(
        endpoint=settings.endpoint or config.openai_compat.endpoint,
        model=settings.model,
        api_key=os.environ.get(config.openai_compat.api_key_env, ""),
        timeout=settings.timeout,
    )


# ── Availability gate ────────────────────────────────────────────────

#: Cached availability probes, keyed by backend identity so two projects
#: pointing at the same ollama share one answer. Value is (available, when).
_availability: dict[tuple[str, str, str], tuple[bool, float]] = {}


def reset_availability_cache() -> None:
    """Clear the cached availability probes (tests, and config reloads)."""
    _availability.clear()


def embedding_backend_available(config: Config) -> bool:
    """Whether the embedding backend is reachable, with a cached probe.

    The default config is ``enabled = true, backend = "ollama"``, so on any
    machine that never pulled an embedding model every extraction would
    otherwise queue a job that fails three times and lands in `failed` —
    quietly tripping the pipeline-health banner fleet-wide. Gating the
    enqueue on a real probe keeps the queue clean instead.

    Negative results are cached for ``AVAILABILITY_RECHECK_SECONDS`` so a
    backend that comes up later is picked up without a restart, and a
    backend that is down does not get probed on every single extraction.
    Positive results are cached indefinitely: if a healthy backend later
    dies, the job itself fails and the queue's backoff handles it.
    """
    settings = config.embeddings
    if not settings.enabled or settings.backend == "none":
        return False

    embedder = create_embedder(config)
    if embedder is None:
        return False

    key = (
        settings.backend,
        getattr(embedder, "endpoint", ""),
        settings.model,
    )
    cached = _availability.get(key)
    now = time.monotonic()
    if cached is not None:
        available, checked_at = cached
        if available or (now - checked_at) < AVAILABILITY_RECHECK_SECONDS:
            return available

    available = embedder.is_available()
    _availability[key] = (available, now)
    if not available:
        logger.warning(
            "Embedding backend unavailable (%s, model=%s at %s) — entity "
            "embeddings will not be queued and search stays FTS-only. "
            "For ollama run: ollama pull %s. Re-checking in %d minutes.",
            settings.backend, settings.model, key[1], settings.model,
            int(AVAILABILITY_RECHECK_SECONDS // 60),
        )
    else:
        logger.info(
            "Embedding backend available (%s, model=%s)",
            settings.backend, settings.model,
        )
    return available


def enqueue_embeddings(
    queue: JobQueue,
    config: Config,
    entity_ids: list[str],
    project_id: str,
) -> str | None:
    """Queue an embedding job for newly-created entities, if that makes sense.

    Single choke point for every entity-creation path (LLM extraction,
    directly-typed events, re-extraction) so the enabled check and the
    availability gate can never be applied inconsistently. Returns the job
    ID, or None when nothing was queued.
    """
    if not entity_ids or not config.embeddings.enabled:
        return None
    if not embedding_backend_available(config):
        return None
    return queue.enqueue(
        EMBED_JOB_TYPE,
        {"entity_ids": list(entity_ids), "project_id": project_id},
    )


# ── Worker ───────────────────────────────────────────────────────────


class EntityEmbedder:
    """Handler for ``embed_entities`` jobs.

    Mirrors the EntityExtractor/Summarizer contract that ``WorkerRunner``
    relies on: ``process_job`` handles one already-claimed job and raises
    on failure (so the queue owns retry/backoff), while ``process_pending``
    drains any remaining jobs and owns their complete/fail itself.
    """

    def __init__(
        self,
        db: Database,
        config: Config,
        embedder: Embedder | None = None,
    ) -> None:
        self.db = db
        self.config = config
        self.queue = JobQueue(db)
        self.embedder = (
            embedder if embedder is not None else create_embedder(config)
        )
        # Stored vectors are keyed by model *and* document prefix, so a
        # prefix change invalidates the old corpus instead of silently
        # mixing incompatible vectors (see embedding_model_key).
        self.model_key = embedding_model_key(config)

    # ── Enqueue ──────────────────────────────────────────────────────

    def enqueue(self, entity_ids: list[str], project_id: str) -> str:
        """Queue an embedding job for freshly-created entities.

        Unconditional by design — this is the explicit "embed these" call.
        Automatic entity-creation paths go through ``enqueue_embeddings``,
        which applies the enabled check and the availability gate.
        """
        return self.queue.enqueue(
            EMBED_JOB_TYPE,
            {"entity_ids": list(entity_ids), "project_id": project_id},
        )

    # ── Job processing ───────────────────────────────────────────────

    def process_job(self, job: Any) -> int:
        """Embed one already-claimed job's entities. Returns the count written.

        Raises on backend failure so ``WorkerRunner.process_one`` can fail
        the job and let the queue's exponential backoff hold it until the
        backend recovers.
        """
        if self.embedder is None:
            return 0

        entity_ids = job.payload.get("entity_ids", [])
        if not entity_ids:
            return 0

        from callmem.core.repository import Repository

        repo = Repository(self.db)
        pending = [
            row for row in repo.get_entities_by_ids(entity_ids)
            if not self._already_embedded(repo, row["id"])
        ]
        if not pending:
            return 0

        written = 0
        batch_size = max(1, self.config.embeddings.batch_size)
        for start in range(0, len(pending), batch_size):
            written += self._embed_rows(repo, pending[start:start + batch_size])
        return written

    def process_pending(self) -> int:
        """Drain every remaining ``embed_entities`` job. Returns rows written."""
        total = 0
        while True:
            job = self.queue.dequeue(EMBED_JOB_TYPE)
            if job is None:
                break
            try:
                total += self.process_job(job)
                self.queue.complete(job.id)
            except Exception as exc:
                logger.error("Embedding job %s failed: %s", job.id, exc)
                self.queue.fail(job.id, str(exc))
        return total

    # ── Backfill ─────────────────────────────────────────────────────

    def backfill(
        self,
        project_id: str,
        batch_size: int | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Embed existing entities that have no vector for the current model.

        Resumable by construction: each pass re-queries what is still
        missing, so an interrupted run simply picks up where it left off.
        Returns ``{embedded, remaining, disabled, model, stalled}``.
        """
        from callmem.core.repository import Repository

        repo = Repository(self.db)
        if self.embedder is None:
            return {
                "embedded": 0,
                "remaining": repo.count_entities_missing_embeddings(
                    project_id, "",
                ),
                "disabled": True,
                "model": None,
                "stalled": False,
            }

        size = max(1, batch_size or self.config.embeddings.batch_size)
        embedded = 0
        stalled = False

        while limit is None or embedded < limit:
            want = size if limit is None else min(size, limit - embedded)
            rows = repo.list_entities_missing_embeddings(
                project_id, self.model_key, limit=want,
            )
            if not rows:
                break
            written = self._embed_rows(repo, rows)
            if written == 0:
                # The rows are still missing, so the next pass would fetch
                # exactly the same ones — forever. Stop loudly instead.
                stalled = True
                logger.error(
                    "Backfill stalled: the backend accepted %d entity text(s) "
                    "but produced no usable vectors. Giving up this run to "
                    "avoid an endless retry loop.", len(rows),
                )
                break
            embedded += written

        return {
            "embedded": embedded,
            "remaining": repo.count_entities_missing_embeddings(
                project_id, self.model_key,
            ),
            "disabled": False,
            "model": self.model_key,
            "stalled": stalled,
        }

    # ── Internals ────────────────────────────────────────────────────

    def _already_embedded(self, repo: Any, entity_id: str) -> bool:
        if self.embedder is None:
            return True
        row = repo.get_embedding(entity_id)
        return row is not None and row["model"] == self.model_key

    def _embed_rows(self, repo: Any, rows: list[dict[str, Any]]) -> int:
        """Embed a batch of entity rows and persist the vectors."""
        if self.embedder is None or not rows:
            return 0

        prefix = self.config.embeddings.document_prefix
        texts = [prefix + entity_embedding_text(row) for row in rows]
        vectors = self.embedder.embed(texts)
        if vectors is None:
            msg = (
                f"embedding backend returned no vectors for "
                f"{len(texts)} entity text(s)"
            )
            raise RuntimeError(msg)

        # strict: backends validate count and per-vector shape before
        # returning, so a mismatch here is a contract violation worth
        # failing the job over.
        written = 0
        for row, vector in zip(rows, vectors, strict=True):
            if not vector:
                continue
            repo.upsert_embedding(
                row["id"], self.model_key, len(vector), pack_vector(vector),
            )
            written += 1
        return written
