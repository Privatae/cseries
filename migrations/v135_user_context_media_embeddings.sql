-- v135: media embeddings — their own column family on user_context.
--
-- Pictures and video embed the same way text does —
-- the user points a role at a model on their own network or at a provider
-- endpoint they hold a key for. Unset is "not set up yet", never an outage.
--
-- WHY THESE ARE NOT THE TEXT COLUMNS. A media model's vectors live in a
-- DIFFERENT space from the text model's, even at the same dimension. Writing
-- them into embedding/embedding_768/... would split-brain search exactly the
-- way memory_engine.py:49-54 warns two text models in one column would: the
-- nearest neighbours of a text query would be a mix of two incomparable
-- spaces, and nothing in the row would say which. So: own columns, own model
-- stamp, and every TEXT coverage query keeps NOT_FILE_KIND_SQL
-- (server/routes/context.py:85) while every MEDIA coverage query requires it.
--
-- THE DIMS. Chosen from what the candidate models actually output, not from
-- the text lane's set:
--   512  — X-CLIP, CLIP ViT-B (the self-hosted video pick)
--   768  — SigLIP 2 base, nomic-embed-vision (shares nomic-embed-text's space)
--   1024 — jina-clip-v2, Voyage multimodal-3, SigLIP 2 large
--   1152 — SigLIP 2 so400m
--   1536 — Cohere embed v4
--   3072 — Gemini Embedding native
-- A model whose native dim is off this grid is handled the way the text lane
-- already handles it (server/routes/embedding.py:456-471): MRL truncation to
-- the best fitting column dim, and ONLY a dim the endpoint provably serves at
-- is persisted. No pin, no fallback default.
--
-- ALL HALFVEC, unlike the text family's vector/halfvec split. One type means
-- one cast rule and one opclass for the whole family, and every dim here is
-- under pgvector's 4000d halfvec HNSW cap — so unlike embedding_4096, no
-- media column is left unindexed. fp16 is the standard storage choice for
-- cosine over normalized embeddings and is already what 2048/2560/4096 use.
--
-- GUARDED, because user_context is created LAZILY (context.py ensure_table) and
-- a host that has never ingested context has no table at all. The v090 lesson:
-- this migration must be a no-op there rather than an error, and the lazy
-- schema carries the same columns so a table created AFTER this migration
-- still gets them. Both paths, or a host created in the gap has no home for a
-- media vector (the v071 2560 bug, verbatim).

DO $$
BEGIN
    IF to_regclass('public.user_context') IS NULL THEN
        RAISE NOTICE 'v135: user_context absent (lazy-create host) — nothing to alter';
        RETURN;
    END IF;

    ALTER TABLE user_context ADD COLUMN IF NOT EXISTS media_embedding_512  halfvec(512);
    ALTER TABLE user_context ADD COLUMN IF NOT EXISTS media_embedding_768  halfvec(768);
    ALTER TABLE user_context ADD COLUMN IF NOT EXISTS media_embedding_1024 halfvec(1024);
    ALTER TABLE user_context ADD COLUMN IF NOT EXISTS media_embedding_1152 halfvec(1152);
    ALTER TABLE user_context ADD COLUMN IF NOT EXISTS media_embedding_1536 halfvec(1536);
    ALTER TABLE user_context ADD COLUMN IF NOT EXISTS media_embedding_3072 halfvec(3072);

    -- Which media model wrote the row's vector. Read for coverage ("N pictures
    -- searchable") and to mark a row stale after a model switch. The column a
    -- vector sits in names its DIM, never its model — two 1024d media models
    -- are as incomparable as two text models in one column.
    ALTER TABLE user_context ADD COLUMN IF NOT EXISTS media_embed_model TEXT;

    -- Frame rows of one clip: which video they came from and where in it.
    -- A picture row has neither. Kept as columns rather than metadata keys
    -- because the coverage and grouping queries filter on them.
    ALTER TABLE user_context ADD COLUMN IF NOT EXISTS media_parent_id INTEGER;
    ALTER TABLE user_context ADD COLUMN IF NOT EXISTS media_offset_ms INTEGER;

    CREATE INDEX IF NOT EXISTS idx_uc_media_512_hnsw
        ON user_context USING hnsw (media_embedding_512 halfvec_cosine_ops)
        WITH (m=16, ef_construction=64);
    CREATE INDEX IF NOT EXISTS idx_uc_media_768_hnsw
        ON user_context USING hnsw (media_embedding_768 halfvec_cosine_ops)
        WITH (m=16, ef_construction=64);
    CREATE INDEX IF NOT EXISTS idx_uc_media_1024_hnsw
        ON user_context USING hnsw (media_embedding_1024 halfvec_cosine_ops)
        WITH (m=16, ef_construction=64);
    CREATE INDEX IF NOT EXISTS idx_uc_media_1152_hnsw
        ON user_context USING hnsw (media_embedding_1152 halfvec_cosine_ops)
        WITH (m=16, ef_construction=64);
    CREATE INDEX IF NOT EXISTS idx_uc_media_1536_hnsw
        ON user_context USING hnsw (media_embedding_1536 halfvec_cosine_ops)
        WITH (m=16, ef_construction=64);
    CREATE INDEX IF NOT EXISTS idx_uc_media_3072_hnsw
        ON user_context USING hnsw (media_embedding_3072 halfvec_cosine_ops)
        WITH (m=16, ef_construction=64);

    -- Frame lookup: every frame of one clip, in order.
    CREATE INDEX IF NOT EXISTS ix_uc_media_parent
        ON user_context(entity_id, media_parent_id, media_offset_ms)
        WHERE media_parent_id IS NOT NULL;
END $$;
