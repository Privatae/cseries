-- v110_project_phase_archived_status.sql
--
-- project_phases gets a real 'archived' state.
--
-- Nothing in the tree could REMOVE a phase. Not the chat tool (project_phase's
-- op enum is list/add_phase/set_phase_spec/add_step/set_step_status), not the
-- REST surface (projects.py has DELETE only for a project and a routing row),
-- not the frontend (workbench-web's api.ts exposes fetchPhases/fetchPhaseSteps
-- and no phase mutation at all). The single removal path in the codebase is the
-- supersede inside build(action='plan') — `DELETE FROM project_phases WHERE
-- project_id=$1 AND status <> 'complete'` — which is all-or-nothing and takes
-- the host's own authored phases with it.
--
-- So a project that pivots accumulates dead phases it cannot clear, and the one
-- lever that clears them destroys the replacement plan too. A project that
-- pivoted kept its old phases, and they made a mess.
--
-- 'archived' is RETIREMENT, not deletion: the row and its steps survive as
-- history, and every query that means "remaining work" skips it (see
-- _PHASE_OPEN_SQL in brain/mastercode/runtime/tools.py — one predicate shared
-- by the build door, the auto-advance chain, the project-complete flip and the
-- halt report, so they cannot drift apart).
--
-- entity_clock.py:193 has ALWAYS filtered phase deadlines with
-- `status NOT IN ('complete','archived')` — it was written against a state the
-- CHECK never permitted. This migration makes that filter mean something.
--
-- Idempotent: drops the named constraint if present, re-adds it with the
-- widened enum. Postgres names an inline CHECK "<table>_<column>_check".
-- Follows v108 exactly.
-- Wrapped, so the table is never briefly unconstrained between the DROP and
-- the ADD. v111 does the same and its comment called that "an improvement on
-- v108's unwrapped form" — which left THIS migration carrying the identical
-- exposure it was describing.
BEGIN;
ALTER TABLE project_phases DROP CONSTRAINT IF EXISTS project_phases_status_check;
ALTER TABLE project_phases ADD CONSTRAINT project_phases_status_check
    CHECK (status IN ('planning','speccing','ready','in_progress','review','complete','failed','archived'));
COMMIT;
