-- v101: multi-dim trigger_embedding columns.
--
-- behavioral_corrections.trigger_embedding is vector(1024) only, so an
-- entity configured with a non-1024 embedding model stored NULL triggers
-- and the constitutional similarity lane sat inactive (the skip is loud;
-- this makes it unnecessary). Mirrors entity_memory's dim
-- columns (v044/v049/v070/v071): vector up to 1024, halfvec above 2000 —
-- the same types mastercode/correction_embeddings.py casts to. No vector
-- indexes: corrections are a small per-entity set (~1.4k rows platform-
-- wide), sequential scan is fine, same as the 1024 column today.
--
-- init-db.sql carries the same columns for fresh pods (the v090 lesson).

BEGIN;

ALTER TABLE behavioral_corrections
    ADD COLUMN IF NOT EXISTS trigger_embedding_768  vector(768),
    ADD COLUMN IF NOT EXISTS trigger_embedding_2048 halfvec(2048),
    ADD COLUMN IF NOT EXISTS trigger_embedding_2560 halfvec(2560),
    ADD COLUMN IF NOT EXISTS trigger_embedding_4096 halfvec(4096);

COMMIT;
