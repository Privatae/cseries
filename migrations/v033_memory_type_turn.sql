-- Migration v033: add 'turn' to entity_memory.memory_type CHECK constraint.
--
-- Renumbered from v022 on 2026-05-23. The previous file
-- v022_memory_type_turn.sql had a version-number collision with
-- v022_voice_phase0_ledger.sql (applied 2026-05-21). The entrypoint
-- deduplicates migrations by integer version, so the constraint
-- update silently skipped. Net effect: memory_type='turn' writes have
-- been failing the CHECK constraint for weeks; entity_memory.turn
-- rows never landed; write_turn_row's "non-fatal" warning fired on
-- every chat. Renumber and apply.
--
-- Verbatim chunks from the synaptic signal() path were being stored as
-- 'summary', which (a) misleadingly named (content is verbatim, not
-- summarized) and (b) collided with the semantics other readers may
-- want to give 'summary' later. The verbatim path's correct type is
-- 'turn' — each row represents a chunked piece of a conversational turn.
--
-- Idempotent. Safe to run multiple times. Additive — does not remove
-- any existing value, so rows already stored as 'summary' remain valid.

BEGIN;

ALTER TABLE entity_memory
    DROP CONSTRAINT IF EXISTS entity_memory_memory_type_check;

ALTER TABLE entity_memory
    ADD CONSTRAINT entity_memory_memory_type_check
    CHECK (memory_type IN (
        'fact','preference','goal','emotion','topic',
        'relationship','decision','unresolved','summary','teaching',
        'moral_precedent','constitutional','legacy','knowledge',
        'turn'
    ));

COMMIT;
