-- Migration v082: add the bot-execution audit columns to FRESH-pod
-- entity_behavioral_log.
--
-- The inverse of v035. That migration patched DRIFTED pods (whose table
-- had only the simple bot-execution shape: bot_name, status, elapsed_ms,
-- error, args, violations) by adding the rich init-db.sql columns. But
-- pods provisioned FROM init-db.sql have the opposite gap: the canonical
-- CREATE TABLE (init-db.sql:353) declares only the rich columns and
-- never had bot_name/status/error/args/elapsed_ms/violations at all.
--
-- Live consequence (observed on a live Host's boot, 2026-07-09): every
-- invariant19 audit write fails ~17x per boot with
--   column "bot_name" of relation "entity_behavioral_log" does not exist
-- Writers that hit the missing columns:
--   server/synaptive_handler.py:558  (invariant19:directive)
--   server/routes/mcp.py:2090        (invariant19 gate audit)
--   brain/mastercode/codebook.py     (invariant19:codebook x4)
--   brain/mastercode/bot_executor.py (_log_execution simple shape)
-- Readers: server/routes/projects.py:792, server/routes/circles.py:1579,
-- scripts/test_* audit gates.
--
-- All of those soft-fail under try/except + log.warning, so every
-- invariant19/bot-execution audit row on a fresh pod has been silently LOST
-- (same Invariant 18 exposure v035 documented for drifted pods).
--
-- Fix is additive and mirrors v035's discipline: every column nullable,
-- ADD COLUMN IF NOT EXISTS, safe on every pod generation (drifted pods
-- already have these columns — no-op there).

BEGIN;

ALTER TABLE entity_behavioral_log
    ADD COLUMN IF NOT EXISTS bot_name    VARCHAR(100);

-- Writer status values are free-form ('allowed','denied','clamped',
-- 'missing','completed', ...). No CHECK — match the drifted-pod shape.
ALTER TABLE entity_behavioral_log
    ADD COLUMN IF NOT EXISTS status      VARCHAR(30);

ALTER TABLE entity_behavioral_log
    ADD COLUMN IF NOT EXISTS error       TEXT;

ALTER TABLE entity_behavioral_log
    ADD COLUMN IF NOT EXISTS args        JSONB;

ALTER TABLE entity_behavioral_log
    ADD COLUMN IF NOT EXISTS elapsed_ms  INTEGER;

ALTER TABLE entity_behavioral_log
    ADD COLUMN IF NOT EXISTS violations  JSONB;

-- Drifted pods carry `ts` (their original timestamp column); fresh pods
-- use created_at. Add ts defaulted to now() so any writer that still
-- sets/reads ts works on fresh pods too.
ALTER TABLE entity_behavioral_log
    ADD COLUMN IF NOT EXISTS ts          TIMESTAMPTZ DEFAULT now();

-- The audit gates (scripts/test_*) and the projects/circles activity
-- readers all filter on bot_name.
CREATE INDEX IF NOT EXISTS idx_behavioral_log_bot
    ON entity_behavioral_log (bot_name) WHERE bot_name IS NOT NULL;

COMMIT;
