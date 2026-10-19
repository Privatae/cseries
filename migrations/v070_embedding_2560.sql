-- Migration v070: 2560-dim content embedding column (qwen3-embedding:4b).
--
-- qwen3-embedding:4b outputs 2560 dims. Stored as HALFVEC so it can be HNSW-indexed: pgvector's
-- HNSW supports halfvec up to 4000 dims, while plain `vector` HNSW caps at 2000.
-- halfvec(2560) with an HNSW index has run at 2560 in production — the earlier
-- "pgvector can't index >2000d" note
-- was wrong; that limit applies only to the `vector` type, not halfvec.
--
-- New rows set embedding_model at write time; nothing to backfill (the column is
-- new). The DO-block converts a pre-existing vector(2560) column to halfvec in
-- case an earlier build added it as vector.

BEGIN;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'entity_memory'
          AND column_name = 'embedding_2560'
          AND udt_name = 'vector'
    ) THEN
        ALTER TABLE entity_memory
            ALTER COLUMN embedding_2560 TYPE halfvec(2560)
            USING embedding_2560::halfvec(2560);
    END IF;
END $$;

ALTER TABLE entity_memory
    ADD COLUMN IF NOT EXISTS embedding_2560 halfvec(2560);

-- HNSW (halfvec, cosine). Column is new/empty everywhere, so the build is instant.
CREATE INDEX IF NOT EXISTS idx_entity_memory_embedding_2560_hnsw
    ON entity_memory USING hnsw (embedding_2560 halfvec_cosine_ops)
    WITH (m = 16, ef_construction = 64);

COMMIT;
