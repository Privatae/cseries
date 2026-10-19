-- Migration v085: add entity_interactions.operator_id to older pods.
--
-- init-db.sql:394 declares entity_interactions.operator_id (INTEGER, NULL on
-- a personal computer), and codebook.record_interaction (codebook.py:620-628)
-- INSERTs it on EVERY turn. But DBs initialized before init-db.sql gained the
-- column never had it, and no migration ever added it — so on those older
-- entities the interaction INSERT fails:
--   column "operator_id" of relation "entity_interactions" does not exist
-- session.py:3190-3201 soft-fails under try/except + log.warning, so every
-- conversation turn on an older pod silently LOSES its interaction-audit row
-- ("Interaction logging failed" once per turn).
--
-- Fix is additive and mirrors the neighboring migrations' discipline: nullable,
-- ADD COLUMN IF NOT EXISTS, safe on every pod generation (fresh pods already
-- have the column from init-db.sql — no-op there). Shape matches init-db.sql.

BEGIN;

ALTER TABLE entity_interactions
    ADD COLUMN IF NOT EXISTS operator_id INTEGER;

COMMIT;
