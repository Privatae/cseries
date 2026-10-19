-- Migration v044: multi-dim embedding columns + per-row model tag.
--
-- Users pick from the embedding server's full catalog
-- (bge-m3, mxbai-embed-large, nomic-embed-text, snowflake-arctic-embed,
-- qwen3-embedding 0.6b/7.6B). Output dims differ: 768 (nomic), 1024
-- (most), 4096 (qwen3 7.6B). pgvector columns are fixed-dim, so one
-- column can't hold all variants.
--
-- Shape: keep the existing `embedding vector(1024)` for the 1024-dim
-- models (most of the catalog). Add two more columns for the other
-- supported dims. Add `embedding_model` so retrieval knows which
-- column to query for any given row, and so backfill jobs know which
-- rows need re-embedding when the user switches models.
--
-- Existing rows: backfill `embedding_model = 'mxbai-embed-large'`.
-- Existing pods have been embedding with mxbai since v0.12.x; that's
-- the historical default. New rows set the column at write time.
--
-- Indexes: HNSW per dim column. Already exists on `embedding` (1024d);
-- add matching indexes on embedding_768 + embedding_4096.

BEGIN;

ALTER TABLE entity_memory
    ADD COLUMN IF NOT EXISTS embedding_768  vector(768),
    ADD COLUMN IF NOT EXISTS embedding_4096 vector(4096),
    ADD COLUMN IF NOT EXISTS embedding_model TEXT;

-- Backfill: existing rows were embedded with mxbai-embed-large (the
-- in-pod Ollama default). Any future row will set this at write time.
UPDATE entity_memory
   SET embedding_model = 'mxbai-embed-large'
 WHERE embedding_model IS NULL
   AND embedding IS NOT NULL;

-- HNSW indexes for the new dim columns. Only created when the column
-- has at least one populated row — Postgres permits empty index but the
-- planner picks them up at query time. Cosine distance to match the
-- existing 1024d index pattern.
CREATE INDEX IF NOT EXISTS idx_entity_memory_embedding_768_hnsw
    ON entity_memory USING hnsw (embedding_768 vector_cosine_ops)
    WHERE embedding_768 IS NOT NULL;

-- No HNSW index on embedding_4096 — pgvector HNSW caps at 2000 dim
-- (the qwen3-embedding 7.6B model is 4096d). Sequential scan is the
-- trade-off for users who pick the high-dim model. A future migration
-- can switch to halfvec(4096) with HNSW (4000d limit applies to bytes,
-- so half-precision fits). For now, the column exists and writes work.

CREATE INDEX IF NOT EXISTS idx_entity_memory_embedding_model
    ON entity_memory (entity_id, embedding_model)
    WHERE embedding_model IS NOT NULL;

COMMIT;
