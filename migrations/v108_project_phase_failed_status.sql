-- v108_project_phase_failed_status.sql
--
-- project_phases gets a real 'failed' state (host-tooling cleanup, 2026-08-19).
--
-- The CHECK on project_phases.status had no failure value, so a failed pool
-- build wrote its phase back to 'ready' (brain/mastercode/runtime/tools.py
-- _PHASE_FAILED = "ready") — pixel-identical in the Workbench workflow pane to
-- a phase that was never attempted. The failure evidence lived only on the
-- factory_build registry row; the phase itself said nothing.
-- It read as completed, nothing followed up, and the failure stayed silent.
--
-- 'failed' is terminal-until-retried: build(action='build') still picks the
-- next phase whose status <> 'complete', so a failed phase is re-buildable
-- exactly as before — it just no longer pretends it was never tried.
--
-- Idempotent: drops the named constraint if present, re-adds it with the
-- widened enum. Postgres names an inline CHECK "<table>_<column>_check".
ALTER TABLE project_phases DROP CONSTRAINT IF EXISTS project_phases_status_check;
ALTER TABLE project_phases ADD CONSTRAINT project_phases_status_check
    CHECK (status IN ('planning','speccing','ready','in_progress','review','complete','failed'));
