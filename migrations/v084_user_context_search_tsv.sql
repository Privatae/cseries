-- v084 — Context Explorer: lexical search leg + the 2560 lazy-create gap.
--
-- 1. user_context.search_tsv — the FTS column behind hybrid context search
--    (semantic + lexical, RRF-fused like brain/codesearch). It is populated at
--    WRITE TIME by the insert paths (server/routes/context.py), NEVER as a
--    GENERATED column: an externalized row (storage_backend != 'pg') holds only
--    the external marker in `content`, so a generated column would lexically
--    index the marker instead of the real bytes.
--
--    Backfill covers pg-resident rows only (their inline content IS the real
--    bytes). Externalized rows backfill lazily on their next content write —
--    referencing the marker here is impossible anyway (it contains NUL).
--
-- 2. user_context.embedding_2560 — v071 added this column as an ALTER on
--    EXISTING tables, but the lazy-create schema (context.py USER_CONTEXT_SCHEMA)
--    never gained it: a host whose user_context was first created after v071
--    has no 2560 slot, so the active-column router's writes for a 2560 model
--    (qwen3-embedding:4b — the onboarding model) fail. The runtime schema now
--    includes it; this migration heals tables created in the gap window.
--
-- ORDERING GUARD (the v078 pattern): user_context is created LAZILY at runtime
-- (context.ensure_table). On a host that has never used the context corpus the
-- table is absent at boot-migration time — everything here no-ops behind a
-- to_regclass guard and the runtime schema self-heals on first creation.

BEGIN;

DO $$
BEGIN
    IF to_regclass('public.user_context') IS NULL THEN
        RAISE NOTICE 'v084: user_context absent (lazy-create) — no-op';
        RETURN;
    END IF;

    -- 1) lexical leg
    ALTER TABLE user_context ADD COLUMN IF NOT EXISTS search_tsv tsvector;
    CREATE INDEX IF NOT EXISTS ix_uc_search_tsv
        ON user_context USING gin (search_tsv);

    -- Backfill pg-resident rows (inline content = real bytes). Capped input
    -- mirrors the runtime _TSV_SQL expression exactly.
    UPDATE user_context
       SET search_tsv = to_tsvector('english',
               left(coalesce(title, '') || ' ' || content, 200000))
     WHERE search_tsv IS NULL
       AND coalesce(storage_backend, 'pg') = 'pg'
       AND content IS NOT NULL;

    -- 2) the 2560 lazy-create gap (mirrors v071's type + index exactly)
    ALTER TABLE user_context ADD COLUMN IF NOT EXISTS embedding_2560 halfvec(2560);
    CREATE INDEX IF NOT EXISTS idx_user_context_embedding_2560_hnsw
        ON user_context USING hnsw (embedding_2560 halfvec_cosine_ops)
        WITH (m=16, ef_construction=64);
END $$;

COMMIT;
