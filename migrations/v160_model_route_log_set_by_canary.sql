-- v160 — model_route_log.set_by admits 'canary' (2026-09-18).
--
-- v158 declares the CHECK with 'canary' already in it, and v158 has shipped
-- to no entity (it is on no released branch). But a development or smoke
-- database booted from this branch BEFORE the canary landed carries the
-- four-value CHECK, and migrations are applied once: on such a database
-- every canary row would fail the CHECK and be swallowed by the writer
-- (inference._log_route_apply logs and continues), losing the audit row
-- while the route still changed. This
-- re-asserts the constraint wherever the table exists, idempotently.
--
-- Guarded: nothing to do if the table is absent (v158 not yet applied — it
-- will create the table with the five-value CHECK itself).

DO $$
BEGIN
    IF to_regclass('public.model_route_log') IS NULL THEN
        RAISE NOTICE 'v160: model_route_log absent — nothing to re-assert';
        RETURN;
    END IF;
    ALTER TABLE model_route_log DROP CONSTRAINT IF EXISTS model_route_log_set_by_check;
    ALTER TABLE model_route_log ADD CONSTRAINT model_route_log_set_by_check
        CHECK (set_by IN ('user', 'decision', 'host', 'host_revert', 'canary'));
END $$;
