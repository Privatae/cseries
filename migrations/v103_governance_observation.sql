-- Migration v103: add 'governance_observation' to entity_memory.memory_type
-- CHECK constraint.
--
-- Host-governance floor (DoR 2026-08-03, §3.4/§3.5 — SB 243 in force
-- 2026-01-01, NY GBL §1700): floor events (the crisis gate fired, the
-- referral was shown) persist as content-free entity_memory rows —
-- content holds only {"event","ts"} json, never user text — so the
-- operator can PROVE the floor ran (counts + timestamps feed the 2027-07
-- CA report). Without this the insert fails the CHECK constraint, the
-- same bug class v033/v034 fixed for 'turn'/'research'.
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
        'relationship','decision','unresolved','summary',
        'skill_memory',
        'moral_precedent','constitutional','legacy','knowledge',
        'turn','research','governance_observation'
    ));

COMMIT;
