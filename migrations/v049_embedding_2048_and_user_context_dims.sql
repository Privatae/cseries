-- Migration v049: 2048-dim content column + user_context multi-dim parity.
--
-- Ship 10b (multi-dim embedding interchangeability). Entity CONTENT
-- embeddings must work for any catalog model dim (768/1024/2048/4096),
-- routed by the entity's active model. Two gaps this migration closes:
--
--   1. entity_memory had 768/1024/4096 (v044) but no 2048 slot. No catalog
--      model emits 2048 today, but the column-router whitelist now includes
--      2048 → embedding_2048, so the column must exist for that route to be
--      storable the moment a 2048 model is added. Added here, idempotently.
--
--   2. user_context only had a single untyped `embedding vector` column, so a
--      non-1024 model's user-context vectors had nowhere dim-correct to land.
--      Mirror entity_memory's dim columns onto user_context so import / search
--      / promote route to the same active column as entity_memory.
--
-- Indexes: HNSW per dim column WHERE pgvector can build one. pgvector's HNSW
-- caps at 2000 dimensions, so embedding_2048 and embedding_4096 get NO HNSW
-- (sequential scan) — the same accepted trade-off v044 made for 4096. Only
-- the 768 index is added here (1024 already exists from v044/init-db).
--
-- dimension_vector (10d personality) and dimension_scores are NOT touched.
-- decisions.prompt_embedding / entity_episodes.embedding /
-- behavioral_corrections.trigger_embedding are out of scope.

BEGIN;

-- 1. entity_memory: 2048 content column.
ALTER TABLE entity_memory
    ADD COLUMN IF NOT EXISTS embedding_2048 vector(2048);

-- 2. user_context: mirror entity_memory's dim columns — GUARDED.
-- user_context is RUNTIME-ensured by context.py (created lazily on first use),
-- so it may not exist at migration time (a pod with no context intake never
-- created it — this once crashed a live pod). context.py's own ensure-schema
-- adds these same columns when it creates/opens the table, so skipping here when
-- the table is absent loses nothing. Guard so the migration never fails.
DO $$
BEGIN
    IF to_regclass('public.user_context') IS NOT NULL THEN
        ALTER TABLE user_context
            ADD COLUMN IF NOT EXISTS embedding_768  vector(768),
            ADD COLUMN IF NOT EXISTS embedding_2048 vector(2048),
            ADD COLUMN IF NOT EXISTS embedding_4096 vector(4096);
        -- HNSW for the indexable dims (2048/4096 stay unindexed, >2000d cap).
        CREATE INDEX IF NOT EXISTS idx_user_context_embedding_768_hnsw
            ON user_context USING hnsw (embedding_768 vector_cosine_ops)
            WITH (m=16, ef_construction=64);
    END IF;
END $$;

-- Backfill safety: existing pods may have entity_memory.embedding_model NULL
-- on rows that already carry a 1024 `embedding`. v044 set this; re-assert it
-- idempotently so coverage / re-embed targeting stays correct.
UPDATE entity_memory
   SET embedding_model = 'mxbai-embed-large'
 WHERE embedding_model IS NULL
   AND embedding IS NOT NULL;

COMMIT;
