-- Migration v035: restore entity_behavioral_log's rich audit columns.
--
-- Discovered during Ship 5 verification: the deployed entity_behavioral_log
-- on production pods has only the simpler bot-execution columns
-- (bot_name, status, elapsed_ms, error, args, violations) and is missing
-- every other column that init-db.sql declares — event_type, severity,
-- auto_action, trigger_source, trigger_detail, weights_before/after,
-- drift_from_seed, action_result, debug_snapshot, debug_diagnosis,
-- resolved/resolved_at/resolved_by/resolution_notes, created_at.
--
-- Six call sites in the running code (bot_executor._log_execution,
-- codebook.py [x4], runtime/session.py:3452, runtime/skill_events.py:332,
-- routes/brain.py:982) INSERT into those missing columns. They have
-- been failing silently under try/except + log.warning since the
-- schema drift was introduced. Every drift event, violation, reset,
-- envelope_breach, skill_practiced, and bot_execution audit row has
-- been LOST.
--
-- An earlier one-off migration script explicitly flagged this as a 'drifted' table
-- with the note "0 rows; only schema diff matters" — the schema was
-- never reconciled.
--
-- Invariant 18 (Complete Audit Trail) requires this audit. Fix is
-- additive: add back every missing column as nullable so existing
-- (bot_name/status/args-shaped) rows stay valid, and new writers fill
-- in the rich fields. No data loss; the simpler columns remain.
--
-- Idempotent: ADD COLUMN IF NOT EXISTS for every add.
-- Safe to run repeatedly. Compatible with both old and new entity pods.

BEGIN;

-- The categorization columns. event_type is the load-bearing one — the
-- CHECK constraint pins the allowed values to match what writers use.
ALTER TABLE entity_behavioral_log
    ADD COLUMN IF NOT EXISTS event_type      VARCHAR(30);

-- The original init-db.sql CHECK constraint listed 10 event types.
-- Some writers in the codebase use names beyond that list (e.g.
-- 'skill_practiced', 'skill_learned', 'skill_outdated' from
-- skill_events.py). We adopt a permissive CHECK that covers BOTH the
-- init-db.sql canonical set AND the skill_events.py additions, so no
-- writer is blocked. Future audits can tighten this.
ALTER TABLE entity_behavioral_log
    DROP CONSTRAINT IF EXISTS entity_behavioral_log_event_type_check;
ALTER TABLE entity_behavioral_log
    ADD CONSTRAINT entity_behavioral_log_event_type_check
    CHECK (event_type IS NULL OR event_type IN (
        'drift','violation','reset','debug_start','debug_complete',
        'restore','suspend','escalation','envelope_breach','bot_execution',
        'skill_practiced','skill_learned','skill_outdated',
        'codebook_drift','codebook_reset','codebook_update'
    ));

ALTER TABLE entity_behavioral_log
    ADD COLUMN IF NOT EXISTS severity        VARCHAR(20) DEFAULT 'info';

ALTER TABLE entity_behavioral_log
    DROP CONSTRAINT IF EXISTS entity_behavioral_log_severity_check;
ALTER TABLE entity_behavioral_log
    ADD CONSTRAINT entity_behavioral_log_severity_check
    CHECK (severity IS NULL OR severity IN ('info','warning','critical','emergency'));

ALTER TABLE entity_behavioral_log
    ADD COLUMN IF NOT EXISTS auto_action     VARCHAR(30);

ALTER TABLE entity_behavioral_log
    DROP CONSTRAINT IF EXISTS entity_behavioral_log_auto_action_check;
ALTER TABLE entity_behavioral_log
    ADD CONSTRAINT entity_behavioral_log_auto_action_check
    CHECK (auto_action IS NULL OR auto_action IN (
        'none','warn','reset_dimension','reset_full',
        'suspend','debug','escalate','ceigas_block'
    ));

-- Source / detail of the event
ALTER TABLE entity_behavioral_log
    ADD COLUMN IF NOT EXISTS trigger_source  TEXT;

ALTER TABLE entity_behavioral_log
    ADD COLUMN IF NOT EXISTS trigger_detail  TEXT;

-- Drift-specific fields (codebook + envelope tracking)
ALTER TABLE entity_behavioral_log
    ADD COLUMN IF NOT EXISTS weights_before  JSONB;

ALTER TABLE entity_behavioral_log
    ADD COLUMN IF NOT EXISTS weights_after   JSONB;

ALTER TABLE entity_behavioral_log
    ADD COLUMN IF NOT EXISTS drift_from_seed JSONB;

-- Action-outcome fields
ALTER TABLE entity_behavioral_log
    ADD COLUMN IF NOT EXISTS action_result   JSONB;

ALTER TABLE entity_behavioral_log
    ADD COLUMN IF NOT EXISTS debug_snapshot  JSONB;

ALTER TABLE entity_behavioral_log
    ADD COLUMN IF NOT EXISTS debug_diagnosis TEXT;

-- Resolution-tracking fields
ALTER TABLE entity_behavioral_log
    ADD COLUMN IF NOT EXISTS resolved        BOOLEAN DEFAULT false;

ALTER TABLE entity_behavioral_log
    ADD COLUMN IF NOT EXISTS resolved_at     TIMESTAMPTZ;

ALTER TABLE entity_behavioral_log
    ADD COLUMN IF NOT EXISTS resolved_by     VARCHAR(100);

ALTER TABLE entity_behavioral_log
    ADD COLUMN IF NOT EXISTS resolution_notes TEXT;

-- The deployed schema already has `ts` (timestamp with time zone, default
-- now()). Readers in codebook.py:531 use `created_at`. Add `created_at`
-- as an alias-style column that defaults to the same now() so existing
-- queries work. Writers that set neither get both; writers that set
-- one but not the other get the other defaulted.
ALTER TABLE entity_behavioral_log
    ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT now();

-- Backfill created_at from ts for any existing rows ONLY if the
-- drifted-schema `ts` column actually exists on this database. Fresh
-- pods provisioned from init-db.sql never had `ts`; only drifted pods
-- need this backfill. The DO block makes the
-- migration idempotent across both shapes.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_name = 'entity_behavioral_log'
           AND column_name = 'ts'
    ) THEN
        UPDATE entity_behavioral_log
           SET created_at = ts
         WHERE created_at IS NULL AND ts IS NOT NULL;
    END IF;
END $$;

-- Helpful indexes from init-db.sql that were never created on the
-- drifted table.
CREATE INDEX IF NOT EXISTS idx_behavioral_log_type
    ON entity_behavioral_log (event_type);
CREATE INDEX IF NOT EXISTS idx_behavioral_log_severity
    ON entity_behavioral_log (severity);
CREATE INDEX IF NOT EXISTS idx_behavioral_log_created
    ON entity_behavioral_log (created_at);

-- skill_events.py uses a THIRD schema variant: action/action_data/session_id.
-- The author soft-fails on column mismatch (comment at runtime/skill_events.py:327
-- "Schema may vary across deployments; we soft-fail"). Add those columns
-- here so skill_events writes also start succeeding without code change.
ALTER TABLE entity_behavioral_log
    ADD COLUMN IF NOT EXISTS action       TEXT;

ALTER TABLE entity_behavioral_log
    ADD COLUMN IF NOT EXISTS action_data  JSONB;

ALTER TABLE entity_behavioral_log
    ADD COLUMN IF NOT EXISTS session_id   TEXT;

CREATE INDEX IF NOT EXISTS idx_behavioral_log_action
    ON entity_behavioral_log (action) WHERE action IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_behavioral_log_session
    ON entity_behavioral_log (session_id) WHERE session_id IS NOT NULL;

COMMIT;
