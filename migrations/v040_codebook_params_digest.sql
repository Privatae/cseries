-- Ship 14 (2026-05-24) — Invariant 19 extension to codebook_params.
--
-- Ship 12 added codebook_digest to codebook_registry (read-only at
-- runtime). codebook_params is the LIVE write counterpart — decision
-- substrate writes here when the operator approves a parameter
-- override (decisions.py:422-437). Without digest validation a
-- tampered row would silently flow through codebook_engine.load()'s
-- param-fetch block into the active pipeline.
--
-- This migration adds the row_digest column. The Python write wrapper
-- (runtime/codebook_param_digest.py) computes the digest on every
-- INSERT and stores it in the same statement. codebook_engine.load()
-- recomputes per row and drops mismatches with an audit row.
--
-- Backfill: every existing active row (t_valid_until IS NULL) gets a
-- digest computed from the same canonical field set the Python helper
-- uses — entity_id, codebook_id, param_name, value, set_at, set_by,
-- approved_at, approved_by, t_valid_from. Lifecycle-mutable fields
-- (t_valid_until, supersedes, metadata, requires_approval) are
-- excluded so a legitimate close-supersede flow doesn't false-positive.

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_name = 'codebook_params'
           AND column_name = 'row_digest'
    ) THEN
        ALTER TABLE codebook_params
            ADD COLUMN row_digest VARCHAR(64);
    END IF;
END $$;

-- Backfill for active rows (t_valid_until IS NULL). Closed rows keep
-- row_digest=NULL so legacy queries don't need a special case; only
-- the active path is validated.
UPDATE codebook_params
   SET row_digest = encode(
       digest(
           jsonb_build_object(
               'entity_id',     entity_id,
               'codebook_id',   codebook_id,
               'param_name',    param_name,
               'value',         value,
               'set_at',        set_at,
               'set_by',        set_by,
               'approved_at',   approved_at,
               'approved_by',   approved_by,
               't_valid_from',  t_valid_from
           )::text,
           'sha256'
       ),
       'hex'
   )
 WHERE row_digest IS NULL
   AND t_valid_until IS NULL;
