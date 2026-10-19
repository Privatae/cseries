-- v112_harness_usage_phase_id.sql
--
-- harness_usage.phase_id — attribute a coding run to the PHASE it was building
-- (plan-lifecycle audit follow-on, 2026-08-24).
--
-- harness_usage is the live per-run receipt: harness, model, tokens, cost,
-- compute and wall-clock, one row per harness run. Measured on one Host
-- 2026-08-24: 702 rows spanning 2026-05-30..2026-08-23, 647 carrying cost.
-- It is what `harness_costs` reads and it works.
--
-- What it could not answer is "what did this PHASE cost". It records
-- project_id and session_id only, so a phased build's spend rolled up per
-- project with no way to split it by phase — and a phase is the unit the user
-- actually approves and reasons about. (step_provenance, the old per-step
-- provenance DAG, has been frozen since 2026-05-30 when the orchestrator that
-- wrote it was removed; it is NOT being revived — the build path carries no
-- tokens or cost to fill it with, and it would duplicate this table badly.)
--
-- NULLABLE and unconstrained on purpose:
--   - a fan-out or a merge run has no phase, and must stay NULL;
--   - every existing row predates the column;
--   - no FK to project_phases: a phase can be superseded by a re-plan
--     (DELETE ... WHERE status NOT IN ('complete','archived')), and losing the
--     COST record of work that really happened because its phase row was
--     replaced would be a worse bug than a dangling id. The receipt outlives
--     the plan.
--
-- Idempotent (ADD COLUMN IF NOT EXISTS); safe on an entity that has never run
-- a build, and on one carrying 702 rows.
ALTER TABLE harness_usage ADD COLUMN IF NOT EXISTS phase_id INTEGER;

-- Cost-by-phase is the whole point of the column, so index the lookup it adds.
-- Partial: the vast majority of historical rows are NULL and never match.
CREATE INDEX IF NOT EXISTS idx_harness_usage_phase
    ON harness_usage (phase_id, created_at DESC)
 WHERE phase_id IS NOT NULL;
