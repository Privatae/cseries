-- Migration v034: add 'research' to entity_memory.memory_type CHECK constraint.
--
-- Ship 3 (2026-05-23) introduced skills/deep_research.py — the bounded
-- SDK subagent that runs pre-build research and persists findings into
-- entity_memory tagged with memory_type='research'. The persist call
-- failed silently because 'research' wasn't in the CHECK constraint,
-- mirroring exactly the same bug class v033 fixed for 'turn' rows.
-- Caught during live-test of the Ship 3 substrate: 0 research rows
-- landed despite deep_research.run() returning research_ids.
--
-- Idempotent. Safe to run multiple times. Additive — does not remove
-- any existing value.

BEGIN;

ALTER TABLE entity_memory
    DROP CONSTRAINT IF EXISTS entity_memory_memory_type_check;

ALTER TABLE entity_memory
    ADD CONSTRAINT entity_memory_memory_type_check
    CHECK (memory_type IN (
        'fact','preference','goal','emotion','topic',
        'relationship','decision','unresolved','summary','teaching',
        'moral_precedent','constitutional','legacy','knowledge',
        'turn','research'
    ));

COMMIT;
