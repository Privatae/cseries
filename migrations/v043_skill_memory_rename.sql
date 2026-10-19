-- Migration v043: rename memory_type='teaching' → 'skill_memory'.
--
-- Architecture decision: rip "teaching" memories,
-- replace with "skill_memory". Reasons:
--   1. The name "teaching" implied a one-way handler→entity flow that
--      doesn't match how the rows actually work (they're learned rules
--      the harness reads, in the same family as skill .md files).
--   2. Renaming deliberately identifies rotting code. Old
--      callers using the literal string surface immediately at grep time.
--
-- This is a pure rename. No data is dropped. Every existing row keeps
-- its content, embedding, importance, metadata; only the memory_type
-- column value changes.
--
-- Code-side: per-file string renames land in the same image bump.
-- Both sides must move together because the CHECK
-- constraint will reject 'teaching' inserts after this migration runs.
--
-- Idempotent. Safe to re-run: the UPDATE is a no-op once all rows are
-- 'skill_memory', and the CHECK rebuild swaps in the same allowed set.

BEGIN;

-- Drop the old constraint so the UPDATE can rewrite 'teaching' rows.
ALTER TABLE entity_memory
    DROP CONSTRAINT IF EXISTS entity_memory_memory_type_check;

-- Rename in place.
UPDATE entity_memory
   SET memory_type = 'skill_memory'
 WHERE memory_type = 'teaching';

-- Re-add the constraint with 'skill_memory' in the allowed set and
-- 'teaching' removed.
ALTER TABLE entity_memory
    ADD CONSTRAINT entity_memory_memory_type_check
    CHECK (memory_type IN (
        'fact','preference','goal','emotion','topic',
        'relationship','decision','unresolved','summary',
        'skill_memory',
        'moral_precedent','constitutional','legacy','knowledge',
        'turn','research'
    ));

COMMIT;
