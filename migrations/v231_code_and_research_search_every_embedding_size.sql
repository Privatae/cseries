-- v231 (2026-10-16): the Host's code index (code_chunks) and research store
-- (research_chunks) store vectors of every size its memory does, so they
-- follow the embedding model the Host has configured.
--
-- No embedding size is hardcoded. Memory (entity_memory) has one column per size and
-- picks the column from the configured model's size
-- (memory_engine.EMBEDDING_COLUMN_BY_DIM); switching models never re-embeds or
-- drops a vector. v200 gave code_chunks one column, halfvec(2560), and
-- brain/codesearch embedded with its own model to fill it, so a Host whose
-- configured embedder is nomic-embed-text (768) on its own machine could not
-- index code by meaning at all. research_chunks (v211) reused that lane and
-- that one halfvec(2560) column, so it had the same limit.
--
-- One column per size, the set memory supports. `embedding` stays the 2560
-- one under its v200 name. Sizes over 2000 are halfvec, because pgvector's
-- HNSW index takes `vector` up to 2000 dimensions and `halfvec` up to 4000;
-- 4096 is over both limits, so it has no index and is ranked exactly, as
-- entity_memory.embedding_4096 is.
--
--   embedding_768    vector(768)     + HNSW
--   embedding_1024   vector(1024)    + HNSW
--   embedding_2048   halfvec(2048)   + HNSW
--   embedding        halfvec(2560)   + HNSW   (v200, unchanged)
--   embedding_4096   halfvec(4096)   no index
--
-- All of them may be NULL (v224): a chunk is written for keyword search
-- before, or without, any vector. A model switch leaves every vector where it
-- is; the next index pass fills the new model's column (codesearch.indexer).
--
-- research_chunks gets the same columns under the same names
-- (codesearch.store.COLUMN_BY_DIM names both tables' columns). It has no
-- vector index today (v211: a find ranks one project's or one conversation's
-- pages exactly), so none is added for the new ones either.

ALTER TABLE code_chunks ADD COLUMN IF NOT EXISTS embedding_768 vector(768);
ALTER TABLE code_chunks ADD COLUMN IF NOT EXISTS embedding_1024 vector(1024);
ALTER TABLE code_chunks ADD COLUMN IF NOT EXISTS embedding_2048 halfvec(2048);
ALTER TABLE code_chunks ADD COLUMN IF NOT EXISTS embedding_4096 halfvec(4096);

CREATE INDEX IF NOT EXISTS idx_code_chunks_embedding_768_hnsw
    ON code_chunks USING hnsw (embedding_768 vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
CREATE INDEX IF NOT EXISTS idx_code_chunks_embedding_1024_hnsw
    ON code_chunks USING hnsw (embedding_1024 vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
CREATE INDEX IF NOT EXISTS idx_code_chunks_embedding_2048_hnsw
    ON code_chunks USING hnsw (embedding_2048 halfvec_cosine_ops)
    WITH (m = 16, ef_construction = 64);

ALTER TABLE research_chunks ADD COLUMN IF NOT EXISTS embedding_768 vector(768);
ALTER TABLE research_chunks ADD COLUMN IF NOT EXISTS embedding_1024 vector(1024);
ALTER TABLE research_chunks ADD COLUMN IF NOT EXISTS embedding_2048 halfvec(2048);
ALTER TABLE research_chunks ADD COLUMN IF NOT EXISTS embedding_4096 halfvec(4096);
