# WO-47 — Vector/Semantic Search

## Goal

Add embedding-based semantic search alongside the existing FTS5 keyword search so users can find conceptually related entities even when the exact terms don't match (e.g., searching "authentication" finds entities about "login", "JWT", "sessions").

## Background

FTS5 is keyword-based — it requires term overlap. Semantic search uses vector embeddings to find conceptual similarity. claude-mem attempted this with ChromaDB but it was unstable. llm-mem's design docs call for `sqlite-vec` (SQLite extension) to keep everything in a single database file.

This is a Phase 2 feature — the schema was designed to accommodate it, but the implementation was deferred.

## Architecture

### Embedding pipeline

1. When an entity is created/updated, generate an embedding from `title + content`
2. Store the embedding in a `sqlite-vec` virtual table
3. At search time, embed the query and find nearest neighbours
4. Combine with FTS5 results using reciprocal rank fusion (RRF)

### Model options

**Local (Ollama)**:
- `nomic-embed-text` (137M params, 768 dims) — good quality, runs on CPU
- `mxbai-embed-large` (335M params, 1024 dims) — better quality, more RAM

**Local (sentence-transformers)**:
- `all-MiniLM-L6-v2` (22M params, 384 dims) — tiny, fast, decent quality
- `all-mpnet-base-v2` (109M params, 768 dims) — better quality

Recommended default: `nomic-embed-text` via Ollama (already installed for extraction, zero extra setup).

## Deliverables

### 1. Dependencies

Add optional dependency group:

```toml
[project.optional-dependencies]
vector = ["sqlite-vec>=0.1.0"]
```

`sqlite-vec` is a single-file SQLite extension. Install with pip, load with `db.load_extension("vec0")`.

### 2. Schema

```sql
CREATE VIRTUAL TABLE entity_embeddings USING vec0(
    entity_id TEXT PRIMARY KEY,
    embedding FLOAT[768]        -- dimension matches model
);
```

Migration: create table if `sqlite-vec` is available, skip gracefully if not.

### 3. Embedding generation

New module `src/llm_mem/core/embeddings.py`:

```python
class EmbeddingProvider(ABC):
    @abstractmethod
    async def embed(self, text: str) -> list[float]: ...
    
    @abstractmethod
    async def embed_batch(self, texts: list[str]) -> list[list[float]]: ...

class OllamaEmbedding(EmbeddingProvider):
    """Use Ollama's /api/embeddings endpoint."""
    
class SentenceTransformerEmbedding(EmbeddingProvider):
    """Use sentence-transformers library (optional dep)."""
```

### 4. Worker integration

Add an embedding job to the worker pipeline, after extraction:

1. Entity extracted → queue embedding job
2. Embedding worker generates embedding → stores in `entity_embeddings`
3. Runs asynchronously, same as extraction

### 5. Hybrid search

Update the search function to combine FTS5 and vector results:

```python
async def hybrid_search(
    query: str,
    project_id: str,
    fts_weight: float = 0.5,   # configurable
    vec_weight: float = 0.5,
    limit: int = 20,
) -> list[SearchResult]:
```

Use Reciprocal Rank Fusion (RRF):
```
score(d) = Σ 1 / (k + rank_i(d))
```
where `k=60` and `rank_i` is the rank from each source.

### 6. Config

```toml
[embeddings]
enabled = true
provider = "ollama"           # or "sentence_transformers" or "none"
model = "nomic-embed-text"    # Ollama model name
dimensions = 768
```

Setup wizard:
```
── Vector search (optional) ──

  Enable semantic/vector search? [Y/n]:
  Embedding provider:
    1) ollama — Use Ollama (recommended if already running)
    2) sentence_transformers — Local Python library
    3) Skip
  Choice [default: 1]:
```

### 7. Backfill command

```bash
llm-mem embed -p .            # generate embeddings for all entities missing them
llm-mem embed --all -p .      # regenerate all embeddings
llm-mem embed --status -p .   # show embedding coverage stats
```

### 8. MCP search update

`mem_search` gains an optional parameter:

```python
search_mode: Literal["keyword", "semantic", "hybrid"] = "hybrid"
```

Default "hybrid" when embeddings are available, falls back to "keyword" when not.

### 9. Web UI

- Search mode toggle: "Keyword" / "Semantic" / "Hybrid" (radio buttons or dropdown)
- Show relevance scores in search results (optional, debug mode)

## Constraints

- Python 3.10 compatible
- No AI attribution
- `sqlite-vec` is an optional dependency — llm-mem must work without it (FTS5 only)
- Embedding generation must not block the main pipeline — async worker queue
- If Ollama is not running, embedding jobs should retry with backoff (not fail permanently)
- Embedding model should be configurable independently from the extraction model
- Must handle model dimension changes gracefully (if user switches embedding model, offer to re-embed)

## Acceptance criteria

- [ ] `sqlite-vec` loaded and virtual table created when available
- [ ] Entity creation triggers embedding generation
- [ ] Hybrid search combines FTS5 and vector results via RRF
- [ ] `mem_search` supports `search_mode` parameter
- [ ] Backfill command embeds existing entities
- [ ] Graceful fallback to keyword-only when vector deps missing
- [ ] Web UI search mode toggle works
- [ ] Config for embedding provider and model
- [ ] Setup wizard offers vector search setup
- [ ] All existing tests pass
