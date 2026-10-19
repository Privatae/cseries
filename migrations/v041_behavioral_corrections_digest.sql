-- Ship 15 (2026-05-24) — Invariant 19 extension to behavioral_corrections.
--
-- Final substrate table in the Invariant 19 closure series:
--   Ship 10 → entity_registry.directive_hash
--   Ship 12 → codebook_registry.codebook_digest
--   Ship 14 → codebook_params.row_digest
--   Ship 15 → behavioral_corrections.row_digest  ← this one
--
-- behavioral_corrections has the most active write surface of the
-- substrate tables: 6 runtime INSERT sites across worker.py,
-- teaching_deduplicator.py, memory_handlers.py. UPDATEs touch
-- mutable lifecycle fields (severity, times_violated, active,
-- updated_at) that are deliberately EXCLUDED from the digest so an
-- escalation or soft-undo doesn't false-positive every row on next
-- codebook_engine.load().
--
-- Digested fields are the row's IMMUTABLE semantics: who wrote it,
-- what the rule says, the trigger context, when it was created.
-- An attacker who tampers with `correction` text or shifts the rule
-- across entities WILL trip the digest. An entity escalating its
-- own rule severity through the legitimate worker path won't.

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_name = 'behavioral_corrections'
           AND column_name = 'row_digest'
    ) THEN
        ALTER TABLE behavioral_corrections
            ADD COLUMN row_digest VARCHAR(64);
    END IF;
END $$;

-- Backfill for active rows. Field set MUST match
-- runtime/behavioral_correction_digest.py exactly.
UPDATE behavioral_corrections
   SET row_digest = encode(
       digest(
           jsonb_build_object(
               'entity_id',       entity_id,
               'role',             role,
               'correction',       correction,
               'trigger_context',  trigger_context,
               'source_message',   source_message,
               'feedback_type',    feedback_type,
               'confidence',       confidence,
               'origin',           origin,
               'codebook_id',      codebook_id,
               'created_at',       created_at
           )::text,
           'sha256'
       ),
       'hex'
   )
 WHERE row_digest IS NULL
   AND active = true;
