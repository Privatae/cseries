-- Re-add entity_registry.dashboard_tier — v054 dropped it on a WRONG premise.
--
-- v054's comment claimed dashboard_tier had "zero readers". That was incorrect: it
-- is read AND written across the platform — entity enrollment/genesis INSERTs it
-- (brain/mastercode/codebook.py and the platform's genesis code), which also SELECTs it
-- into the genesis payload + UPDATEs it. Dropping it broke the enrollment INSERT
-- ("column dashboard_tier does not exist"). It mirrors license_tier (both written
-- to the same tier value); restore it and backfill from license_tier.
--
-- NOTE: license_tier itself is LOAD-BEARING (capability gating via
-- mastercode.license_tiers is_admin/is_builder, codebook permission tiers, prompt
-- sections) and is NOT a billing duplicate — it must never be dropped either. Phase C
-- removed only its MISUSE as a CEIGAS default-access input, not the column.
-- Idempotent.

ALTER TABLE entity_registry ADD COLUMN IF NOT EXISTS dashboard_tier TEXT;
UPDATE entity_registry SET dashboard_tier = license_tier WHERE dashboard_tier IS NULL;
