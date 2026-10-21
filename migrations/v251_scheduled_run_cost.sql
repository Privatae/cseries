-- v251 — what each scheduled run cost.
--
-- A scheduled task runs with nobody watching, and what it spent was visible
-- only per turn row, so the person could not see what their standing work
-- costs. The offline runner now adds up the cost of each turn a run
-- makes (the same estimate the turn row is stamped with) and records it here;
-- the run's report in the thread says it, and calendar_read op='runs' and the
-- Agents app show it.
--
-- NULL = not priced: an op-lane run (no model), or a run on a route with no
-- per-token price (a subscription plan, a local model). Never 0 for unknown.
-- A run with only SOME turns priced (a failover onto a plan mid-run) has a
-- NULL cost_usd, so a partial sum never reads as the whole.
--
-- priced_usd: what the run's priced turns cost, set even when cost_usd is
-- NULL for the reason above. A job's own budget (payload budget_usd) counts
-- it, so the priced part of a partly priced run is never dropped from what
-- the job has spent.
--
-- Additive and idempotent; forward-only.

BEGIN;

ALTER TABLE calendar_event_runs ADD COLUMN IF NOT EXISTS cost_usd DOUBLE PRECISION;
ALTER TABLE calendar_event_runs ADD COLUMN IF NOT EXISTS priced_usd DOUBLE PRECISION;

COMMIT;
