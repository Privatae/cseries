-- v074 — Live A/B preference channel: (chosen, rejected) DPO pairs (s3-dpo-channels).
--
-- Phase 4b. The Circles A/B route (POST /api/circles/ab) fans the SAME prompt
-- across TWO governed, leak-guarded models and renders both responses side by
-- side. When the user picks a winner (POST /api/circles/ab/choose) we record the
-- (chosen, rejected) pair here AND append a behavioral signal into the existing
-- Training Memory substrate (behavioral_corrections via apply_behavioral_correction)
-- so the host's learning loop is fed live preferences — not just mined transcripts.
--
-- This is a preference-CAPTURE table only: no reward model, no RL. It is the raw
-- DPO corpus (one row per user choice), strictly entity-scoped.
--
-- Idempotent so re-running on a pod that already has the table is a no-op.

BEGIN;

CREATE TABLE IF NOT EXISTS dpo_pairs (
    id              BIGSERIAL PRIMARY KEY,
    entity_id       INTEGER NOT NULL,
    prompt          TEXT NOT NULL,
    chosen_model    TEXT,
    chosen_text     TEXT NOT NULL,
    rejected_model  TEXT,
    rejected_text   TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_dpo_pairs_entity
    ON dpo_pairs (entity_id, created_at DESC);

COMMIT;
