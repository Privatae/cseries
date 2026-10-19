-- v161 — model_route_log.rejected_provider / rejected_model (2026-09-18).
--
-- The (provider, model) a canary revert came FROM, recorded on the canary
-- row itself so the rejection is never inferred from adjacency
-- (runtime/route_canary.py; inference.previous_approved_route reads it).
-- v158 declares the columns for a fresh database. This adds them,
-- idempotently, to a database that applied v158/v160 before the columns
-- existed on this branch — the apply loop records a version once and never
-- re-runs it (entrypoint.sh), so an in-place edit to v160 would have left
-- such a database without them, and every route-log INSERT would then name
-- a column that is not there and be swallowed by the writer: no route
-- history, no canary, every host route change a proposal. Silent. This
-- file is the guard against that.
--
-- Guarded: nothing to do if the table is absent (v158 creates it whole).

DO $$
BEGIN
    IF to_regclass('public.model_route_log') IS NULL THEN
        RAISE NOTICE 'v161: model_route_log absent — nothing to add';
        RETURN;
    END IF;
    ALTER TABLE model_route_log ADD COLUMN IF NOT EXISTS rejected_provider TEXT;
    ALTER TABLE model_route_log ADD COLUMN IF NOT EXISTS rejected_model TEXT;
END $$;
