-- Migration v027: decision prompt embeddings + pattern accrual (Ship 4d-2).
--
-- Adds prompt_embedding to decisions so prompts can cluster by semantic
-- similarity. pattern_id (already in v026) becomes a self-pointer to the
-- cluster exemplar (the first decided row in the cluster). Self-organizing:
--   * On propose: embed prompt, find nearest decided neighbor by cosine.
--     If similarity > threshold (currently 0.80, evaluated in route code),
--     inherit that neighbor's pattern_id; otherwise NULL.
--   * On resolve: if pattern_id IS NULL → row becomes its own exemplar
--     (pattern_id = id). Later similar rows join via the propose-side rule.
--
-- 4d-3 will use pattern_id + the cluster's chosen_key distribution to gate
-- auto-decision confidence. This migration adds the substrate only.
--
-- Index: cosine ANN over prompt_embedding for the propose-side nearest
-- search. ivfflat is fine for the scale we'll see in 4d-2 (decision rate
-- is very low — many orders of magnitude below memory writes); revisit
-- if a single entity ever accrues >100k decisions.
--
-- Idempotent. Existing rows have prompt_embedding=NULL and won't be
-- considered for clustering until they're re-embedded; for 4d-2 nothing
-- pre-existing needs backfilling because decisions just shipped.

BEGIN;

ALTER TABLE decisions
    ADD COLUMN IF NOT EXISTS prompt_embedding vector(1024);

CREATE INDEX IF NOT EXISTS idx_decisions_prompt_embedding
    ON decisions USING ivfflat (prompt_embedding vector_cosine_ops)
    WITH (lists = 50)
 WHERE prompt_embedding IS NOT NULL;

COMMIT;
