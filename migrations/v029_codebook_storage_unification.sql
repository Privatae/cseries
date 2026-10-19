-- Migration v029 (Ship MCB2): unify codebook training storage with memory.
--
-- The codebook_training table was designed against the pre-refactor memory
-- model (skills-as-files, awareness ingestion, entity_training_data cron
-- aggregations). The new memory model collapsed those: turn rows are the
-- training corpus, cron consolidator is the derivation engine, decision
-- substrate is the approval surface, bi-temporal supersession is platform-wide.
--
-- This migration ports codebook_training's writes to their natural homes:
--   data_type='example'|'template'|'pattern'|'association'  → entity_memory
--   data_type='rule'|'constraint'                            → behavioral_corrections
--   data_type='weight'|'threshold' (incl. benchmark_competence) → codebook_params
--
-- Each ported row carries metadata.codebook_training_id for lineage, and
-- metadata.codebook_id (or behavioral_corrections.codebook_id) so the
-- engine reader can fetch "rows relevant to this codebook" from the
-- unified substrate.
--
-- COPY semantics — codebook_training rows stay in place during transition.
-- MCB3 swaps the reader; MCB5 retires the writers + drops the table.
-- Idempotent via NOT EXISTS checks on metadata.codebook_training_id.

BEGIN;

-- ── 1. codebook_params: the small "knobs" table ───────────────────────
-- For data_type='weight'/'threshold' rows. These are codebook parameter
-- overrides (impulse_control_threshold, risk_tolerance, etc.), not facts
-- the entity learned. Lives next to codebooks, not in memory.
--
-- entity_id NULL = platform-default (every entity inherits).
-- Bi-temporal: supersede via UPDATE prev SET t_valid_until=NOW(); new row.
CREATE TABLE IF NOT EXISTS codebook_params (
    id                  BIGSERIAL PRIMARY KEY,
    entity_id           INTEGER REFERENCES entity_registry(id) ON DELETE CASCADE,
    codebook_id         INTEGER NOT NULL REFERENCES codebook_registry(id) ON DELETE CASCADE,
    param_name          TEXT NOT NULL,
    value               JSONB NOT NULL,
    -- 'system' | 'user:<email>' | 'cron:<job>' | 'benchmark' | 'migration:v029'
    set_by              TEXT NOT NULL DEFAULT 'system',
    set_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    requires_approval   BOOLEAN DEFAULT FALSE,
    approved_at         TIMESTAMPTZ,
    approved_by         TEXT,
    -- Bi-temporal lineage (same model as entity_memory)
    t_valid_from        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    t_valid_until       TIMESTAMPTZ,
    supersedes          BIGINT REFERENCES codebook_params(id),
    -- Lineage when ported from codebook_training
    metadata            JSONB DEFAULT '{}'::jsonb NOT NULL
);

-- Live params lookup — hot path for codebook engine
CREATE UNIQUE INDEX IF NOT EXISTS idx_codebook_params_live_entity
    ON codebook_params (entity_id, codebook_id, param_name)
    WHERE t_valid_until IS NULL AND entity_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_codebook_params_live_platform
    ON codebook_params (codebook_id, param_name)
    WHERE t_valid_until IS NULL AND entity_id IS NULL;
CREATE INDEX IF NOT EXISTS idx_codebook_params_pending
    ON codebook_params (entity_id, codebook_id)
    WHERE requires_approval = TRUE AND approved_at IS NULL;
COMMENT ON TABLE codebook_params IS
    'Ship MCB2: per-codebook parameter overrides (knobs, not learned facts). Bi-temporal supersession.';


-- ── 2. behavioral_corrections gets a codebook_id link ─────────────────
-- behavioral_corrections already holds the rules entities are bound by
-- (karpathy, superpowers, security_v1, 1377 unlabeled on one Host). Adding
-- codebook_id lets the engine fetch "rules relevant to this codebook"
-- without a parallel table. NULL = global rule (current behavior).
ALTER TABLE behavioral_corrections
    ADD COLUMN IF NOT EXISTS codebook_id INTEGER REFERENCES codebook_registry(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_behavioral_corrections_codebook
    ON behavioral_corrections (codebook_id, active) WHERE codebook_id IS NOT NULL;


-- ── 3-5. Port codebook_training rows to their new homes ───────────────
-- Wrapped in DO block with table-existence guard. Fresh init (post-MCB5)
-- never creates codebook_training, so the loop is a no-op on new pods.
-- Older pods still have
-- the table — the port runs as designed.
--
-- Mapping:
--   example, template → entity_memory.teaching     + metadata.codebook_id
--   pattern           → entity_memory.fact         + metadata.codebook_id
--   association       → entity_memory.relationship + metadata.codebook_id
--   rule, constraint  → behavioral_corrections     + codebook_id column
--   weight, threshold + source_type='benchmark_competence' → codebook_params

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.tables
         WHERE table_schema = 'public' AND table_name = 'codebook_training'
    ) THEN
        -- Fresh init post-MCB5 — no source table to port from.
        RETURN;
    END IF;

    -- Port to entity_memory
    INSERT INTO entity_memory (entity_id, content, memory_type, importance, metadata, created_at, status)
    SELECT
        ct.entity_id,
        COALESCE(ct.content->>'prose', ct.content::text) AS content,
        CASE ct.data_type
            WHEN 'example'     THEN 'teaching'
            WHEN 'template'    THEN 'teaching'
            WHEN 'pattern'     THEN 'fact'
            WHEN 'association' THEN 'relationship'
        END AS memory_type,
        COALESCE(ct.importance, 1.0),
        jsonb_build_object(
            'codebook_id',          ct.codebook_id,
            'source_data_type',     ct.data_type,
            'codebook_training_id', ct.id,
            'source',               ct.source,
            'source_type',          ct.source_type,
            'ported_at',            NOW()::text,
            'ported_by',            'migration:v029',
            'original_content',     ct.content
        ),
        ct.created_at,
        'active'
      FROM codebook_training ct
     WHERE ct.entity_id IS NOT NULL
       AND ct.data_type IN ('example','template','pattern','association')
       AND NOT EXISTS (
           SELECT 1 FROM entity_memory em
            WHERE em.metadata->>'codebook_training_id' = ct.id::text
       );

    -- Port to behavioral_corrections
    INSERT INTO behavioral_corrections
        (entity_id, correction, severity, active, origin, codebook_id,
         trigger_context, source_message, feedback_type, confidence, times_violated, created_at)
    SELECT
        ct.entity_id,
        COALESCE(ct.content->>'rule', ct.content->>'prose', ct.content::text) AS correction,
        'firm' AS severity,
        TRUE AS active,
        'codebook_training:' || ct.data_type || ':' || COALESCE(ct.source, 'unknown') AS origin,
        ct.codebook_id,
        '' AS trigger_context,
        'ported from codebook_training id=' || ct.id::text AS source_message,
        'codebook_derived' AS feedback_type,
        COALESCE(ct.confidence, 0.7) AS confidence,
        0 AS times_violated,
        ct.created_at
      FROM codebook_training ct
     WHERE ct.entity_id IS NOT NULL
       AND ct.data_type IN ('rule','constraint')
       AND NOT EXISTS (
           SELECT 1 FROM behavioral_corrections bc
            WHERE bc.source_message = 'ported from codebook_training id=' || ct.id::text
       );

    -- Port to codebook_params
    INSERT INTO codebook_params
        (entity_id, codebook_id, param_name, value, set_by, set_at, metadata)
    SELECT
        ct.entity_id,
        ct.codebook_id,
        CASE
            WHEN ct.source_type = 'benchmark_competence' THEN 'benchmark_competence'
            WHEN ct.content ? 'param_name'                THEN ct.content->>'param_name'
            ELSE ct.data_type || ':' || ct.id::text
        END AS param_name,
        ct.content AS value,
        'migration:v029' AS set_by,
        ct.created_at AS set_at,
        jsonb_build_object(
            'source_data_type',     ct.data_type,
            'codebook_training_id', ct.id,
            'source',               ct.source,
            'source_type',          ct.source_type,
            'confidence',           ct.confidence,
            'importance',           ct.importance
        )
      FROM codebook_training ct
     WHERE (
            ct.data_type IN ('weight','threshold')
            OR ct.source_type = 'benchmark_competence'
           )
       AND NOT EXISTS (
           SELECT 1 FROM codebook_params cp
            WHERE cp.metadata->>'codebook_training_id' = ct.id::text
       );
END $$;


COMMIT;
