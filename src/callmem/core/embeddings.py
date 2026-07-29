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


class Embedder(ABC):
    """Minimal embedding backend interface.

    ``embed`` returns one vector per input text, or ``None`` when the
    backend is unreachable or answered with something unusable — callers
    treat ``None`` as "try again later", never as "no results".
    """

    model: str

    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]] | None:
        """Embed a batch of texts, or return None on backend failure."""

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

    def embed(self, texts: list[str]) -> list[list[float]] | None:
        if not texts:
            return []
        try:
            resp = httpx.post(
                f"{self.endpoint}/api/embed",
                json={"model": self.model, "input": texts},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("Ollama embed failed: %s", exc)
            return None

        vectors = data.get("embeddings")
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            logger.warning(
                "Ollama embed returned %s vectors for %d input(s)",
                len(vectors) if isinstance(vectors, list) else "no",
                len(texts),
            )
            return None
        return [[float(x) for x in v] for v in vectors]


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

    def embed(self, texts: list[str]) -> list[list[float]] | None:
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
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("OpenAI-compat embed failed: %s", exc)
            return None

        items = data.get("data")
        if not isinstance(items, list) or len(items) != len(texts):
            logger.warning("OpenAI-compat embed returned an unusable payload")
            return None
        # The spec allows results in any order; `index` is authoritative.
        try:
            ordered = sorted(items, key=lambda d: int(d.get("index", 0)))
            return [[float(x) for x in d["embedding"]] for d in ordered]
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("OpenAI-compat embed payload malformed: %s", exc)
            return None


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

    # ── Enqueue ──────────────────────────────────────────────────────

    def enqueue(self, entity_ids: list[str], project_id: str) -> str:
        """Queue an embedding job for freshly-created entities."""
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
        Returns ``{embedded, remaining, disabled, model}``.
        """
        from callmem.core.repository import Repository

        repo = Repository(self.db)
        if self.embedder is None:
            return {
                "embedded": 0,
                "remaining": len(
                    repo.list_entities_missing_embeddings(project_id, "")
                ),
                "disabled": True,
                "model": None,
            }

        model = self.embedder.model
        size = max(1, batch_size or self.config.embeddings.batch_size)
        embedded = 0

        while limit is None or embedded < limit:
            want = size if limit is None else min(size, limit - embedded)
            rows = repo.list_entities_missing_embeddings(
                project_id, model, limit=want,
            )
            if not rows:
                break
            embedded += self._embed_rows(repo, rows)

        remaining = len(
            repo.list_entities_missing_embeddings(project_id, model, limit=10_000)
        )
        return {
            "embedded": embedded,
            "remaining": remaining,
            "disabled": False,
            "model": model,
        }

    # ── Internals ────────────────────────────────────────────────────

    def _already_embedded(self, repo: Any, entity_id: str) -> bool:
        if self.embedder is None:
            return True
        row = repo.get_embedding(entity_id)
        return row is not None and row["model"] == self.embedder.model

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

        # strict: backends validate the count before returning, so a
        # mismatch here is a contract violation worth failing the job over.
        for row, vector in zip(rows, vectors, strict=True):
            if not vector:
                continue
            repo.upsert_embedding(
                row["id"], self.embedder.model, len(vector), pack_vector(vector),
            )
        return sum(1 for v in vectors if v)
