-- v080 — harness_usage billing tie-back (the BILLABLES.md harness-compute gap fix).
--
-- harness_usage was a pure ComputeView reporting table in the entity's LOCAL PG —
-- metered compute the platform billing path could never see ("metered but never
-- debited"). The compute fee is now billed through the bridge (harness.compute_charge
-- → harness_compute_log in the shared host DB → the same advisory-locked sweep as
-- inference/embed). These columns tie each reporting row to its ledger row:
--
--   entity_id   — this pod's entity (env ENTITY_ID), so exported/aggregated rows
--                 stay attributable outside the pod.
--   billing_ref — the run_id the pod minted for the bridge charge (matches
--                 harness_compute_log.run_id). NULL = the charge call never
--                 succeeded; the billing audit greps for exactly those rows.
--
-- Idempotent, forward-only. compute.py's _ensure_schema carries the same ALTERs
-- (belt-and-suspenders, same as harness_usage/harness_run creation).
BEGIN;

ALTER TABLE IF EXISTS harness_usage ADD COLUMN IF NOT EXISTS entity_id   INTEGER;
ALTER TABLE IF EXISTS harness_usage ADD COLUMN IF NOT EXISTS billing_ref TEXT;

-- harness_usage is created LAZILY by compute.py _ensure_schema — a database
-- that predates it has no table here, the ALTERs above skip, and this index
-- must skip too (an unguarded CREATE INDEX once crashlooped a live Host's
-- update).
DO $$
BEGIN
    IF to_regclass('harness_usage') IS NOT NULL THEN
        CREATE INDEX IF NOT EXISTS idx_harness_usage_billing_ref ON harness_usage (billing_ref);
    END IF;
END $$;

COMMIT;
