-- v100: fix the CHECK-invalid license_tier DEFAULT.
--
-- v042/v046 rewrote the license_tier CHECK (final vocab: standard/pro/
-- enterprise/admin, mastercode/license_tiers.py) but never touched the
-- column DEFAULT — live entities still carry DEFAULT 'free' (pre-v042)
-- and fresh pods DEFAULT 'personal' (stale init-db block), BOTH outside
-- the CHECK. Any bare INSERT INTO entity_registry that omits license_tier
-- dies on the constraint — the landmine that broke the v094 guardrail
-- seed on every pod. 'standard' is the canonical default ("every
-- signup"), the same value v046 collapses unknowns to.
--
-- init-db.sql updated to match in the same change (the v090 lesson).

BEGIN;

ALTER TABLE entity_registry
    ALTER COLUMN license_tier SET DEFAULT 'standard';

COMMIT;
