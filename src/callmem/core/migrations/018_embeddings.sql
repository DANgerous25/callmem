-- callmem schema v18: entity embeddings for hybrid (FTS + vector) retrieval
--
-- One row per entity per embedding model. `vector` is the raw float32 packed
-- little-endian representation (struct '<{dim}f'), so `dim` and `LENGTH(vector)`
-- are always consistent; `dim` is stored explicitly so a query vector produced
-- by a different model can be rejected without unpacking the blob.
--
-- Deliberately a plain table, not a sqlite-vec vec0 virtual table: creating a
-- virtual table here would make this migration — and therefore every callmem
-- database — depend on a loadable SQLite extension at upgrade time. Vector
-- scoring happens in Python over a prefiltered candidate set instead (see
-- retrieval.RetrievalEngine._vector_ranked_ids); measured well under the 200ms
-- budget at 10k entities.
--
-- ON DELETE CASCADE: embeddings are derived data and must never outlive the
-- entity they describe (PRAGMA foreign_keys=ON is set on every connection).

CREATE TABLE IF NOT EXISTS embeddings (
    entity_id  TEXT PRIMARY KEY REFERENCES entities(id) ON DELETE CASCADE,
    model      TEXT NOT NULL,
    dim        INTEGER NOT NULL,
    vector     BLOB NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_embeddings_model ON embeddings(model);
