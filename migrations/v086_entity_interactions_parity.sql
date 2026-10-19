-- Migration v086: bring entity_interactions to init-db.sql parity on older pods.
--
-- v085 added operator_id, but the drifted legacy table (live on a long-lived Host:
-- id, entity_id, session_id, ts, interaction_type, metadata, operator_id)
-- still lacks the rest of the canonical shape (init-db.sql:391-405), so
-- codebook.record_interaction — the table's ONE writer, which INSERTs
-- entity_id, operator_id, interaction_type, summary, tokens_used,
-- active_weights on EVERY turn — keeps failing:
--   column "summary" of relation "entity_interactions" does not exist
-- session.py soft-fails under try/except + log.warning, so every turn's
-- interaction-audit row is still silently LOST on those pods.
--
-- Fix is additive and mirrors v082/v085's discipline: every canonical column
-- the drifted shape lacks, nullable (defaults only where init-db.sql has
-- them), ADD COLUMN IF NOT EXISTS, safe on every pod generation (fresh pods
-- no-op). The legacy extras (session_id, ts, metadata) stay — nothing reads
-- them, and dropping columns is never done in a boot migration. No CHECK on
-- interaction_type retroactively (v082's precedent: match the drifted shape).

BEGIN;

ALTER TABLE entity_interactions
    ADD COLUMN IF NOT EXISTS summary             TEXT;

ALTER TABLE entity_interactions
    ADD COLUMN IF NOT EXISTS tokens_used         INTEGER DEFAULT 0;

ALTER TABLE entity_interactions
    ADD COLUMN IF NOT EXISTS duration_seconds    DOUBLE PRECISION;

ALTER TABLE entity_interactions
    ADD COLUMN IF NOT EXISTS active_weights      JSONB;

ALTER TABLE entity_interactions
    ADD COLUMN IF NOT EXISTS collision_strategy  VARCHAR(30);

ALTER TABLE entity_interactions
    ADD COLUMN IF NOT EXISTS satisfaction_signal DOUBLE PRECISION;

ALTER TABLE entity_interactions
    ADD COLUMN IF NOT EXISTS goal_progress       JSONB;

ALTER TABLE entity_interactions
    ADD COLUMN IF NOT EXISTS created_at          TIMESTAMPTZ DEFAULT now();

-- init-db.sql:408-409 parity — the drifted table predates both indexes.
CREATE INDEX IF NOT EXISTS idx_entity_interactions_entity
    ON entity_interactions (entity_id);
CREATE INDEX IF NOT EXISTS idx_entity_interactions_type
    ON entity_interactions (interaction_type);

COMMIT;
