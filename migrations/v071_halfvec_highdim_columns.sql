-- Migration v071: make the >2000-dim content columns HNSW-indexable (halfvec).
--
-- Unified embedding interchangeability: entity_memory's
-- >2000-dim content columns were added as plain `vector` (v044/v049) and so
-- could never be HNSW-indexed (pgvector caps `vector` HNSW at 2000 dims) — they
-- fell back to sequential scan. pgvector's halfvec HNSW reaches 4000 dims, so
-- convert them to halfvec and index the ones within the cap:
--
--   embedding_2048 -> halfvec(2048) + HNSW   (indexable)
--   embedding_2560 -> halfvec(2560) + HNSW   (added/indexed in v070)
--   embedding_4096 -> halfvec(4096), NO HNSW (4096 > halfvec's 4000d HNSW cap;
--                     sequential scan — only the qwen3 7.6B model hits this)
--
-- These columns are empty fleet-wide today (no entity has run a >1024 model), so
-- the type change + index build are instant. user_context mirrors entity_memory
-- (guarded — the table is created lazily by context.py).

BEGIN;

-- entity_memory: convert existing vector columns to halfvec.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='entity_memory' AND column_name='embedding_2048' AND udt_name='vector') THEN
        ALTER TABLE entity_memory ALTER COLUMN embedding_2048 TYPE halfvec(2048)
            USING embedding_2048::halfvec(2048);
    END IF;
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='entity_memory' AND column_name='embedding_4096' AND udt_name='vector') THEN
        ALTER TABLE entity_memory ALTER COLUMN embedding_4096 TYPE halfvec(4096)
            USING embedding_4096::halfvec(4096);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_entity_memory_embedding_2048_hnsw
    ON entity_memory USING hnsw (embedding_2048 halfvec_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- user_context: mirror (guarded — runtime-created table).
DO $$
BEGIN
    IF to_regclass('public.user_context') IS NOT NULL THEN
        IF EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='user_context' AND column_name='embedding_2048' AND udt_name='vector') THEN
            ALTER TABLE user_context ALTER COLUMN embedding_2048 TYPE halfvec(2048)
                USING embedding_2048::halfvec(2048);
        END IF;
        IF EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='user_context' AND column_name='embedding_4096' AND udt_name='vector') THEN
            ALTER TABLE user_context ALTER COLUMN embedding_4096 TYPE halfvec(4096)
                USING embedding_4096::halfvec(4096);
        END IF;
        ALTER TABLE user_context ADD COLUMN IF NOT EXISTS embedding_2560 halfvec(2560);
        CREATE INDEX IF NOT EXISTS idx_user_context_embedding_2048_hnsw
            ON user_context USING hnsw (embedding_2048 halfvec_cosine_ops)
            WITH (m = 16, ef_construction = 64);
        CREATE INDEX IF NOT EXISTS idx_user_context_embedding_2560_hnsw
            ON user_context USING hnsw (embedding_2560 halfvec_cosine_ops)
            WITH (m = 16, ef_construction = 64);
    END IF;
END $$;

COMMIT;
