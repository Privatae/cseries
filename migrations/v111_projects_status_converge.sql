-- v111_projects_status_converge.sql
--
-- projects.status: converge long-lived entities onto the declared schema
-- (plan-lifecycle audit, 2026-08-24).
--
-- THE DRIFT. init-db.sql declares:
--     status TEXT NOT NULL DEFAULT 'planning'
--            CHECK (status IN ('planning','active','review','complete','archived'))
-- but that CREATE TABLE only ever runs on a FRESH entity DB. A long-lived
-- entity kept the pre-Projects-V2 column. Measured 2026-08-24:
--     old entity:  DEFAULT 'inactive', NO check constraint
--     new entity:  DEFAULT 'planning', projects_status_check present
-- Six ALTER TABLE projects migrations exist (v011, v075 x3, v081, v109) and
-- every one is ADD COLUMN — none ever touched status, its default, or its
-- constraint.
--
-- WHAT THAT COSTS. Nothing writes the literal 'inactive' anywhere in the tree;
-- it is purely that column default. So on an old entity a project created
-- through the REST route, order_software, or the URL-import path is born
-- 'inactive' — a value not in the declared enum — while the same code on a new
-- entity produces 'planning'. A project shown as inactive while someone is
-- working in it is confusing.
--
-- It is also sticky: nothing moves a project off it. _run_phase_build writes
-- 'complete' when the last phase lands, and the plan supersede writes 'active'
-- only WHERE status='complete'. So a project sits 'inactive' from creation
-- until its first fully-completed build.
--
-- And it is load-bearing: the ship gate compares against the literal
-- 'complete' (_pack_and_sign_project), on a column that on old entities has no
-- constraint at all — update_project_service writes `status` verbatim with no
-- allowlist, and list_projects' own parameter description advertises
-- 'inactive' as a real status. A host writing it succeeds silently on an old
-- entity and raises a CHECK violation on a new one: the failure is invisible
-- on the operator's own entities and appears only for new users.
--
-- SAFETY. A failed migration aborts entity startup (entrypoint.sh:375-378), so
-- this cannot be allowed to fail on data. The backfill therefore maps ANY
-- value outside the target enum to 'planning' — not just the 'inactive' this
-- was written for — so an unmeasured value on some entity cannot wedge its
-- boot. Fleet at time of writing holds only active/complete/archived/inactive.
--
-- 'planning' is the right landing for 'inactive': it means created-but-not-yet
-- -built, which is exactly what those projects are (213 planned-never-built,
-- 207 planned-partially-built).
--
-- Idempotent, and correct on an entity that is ALREADY converged: the UPDATE
-- matches zero rows, SET DEFAULT is a no-op, and the constraint is dropped and
-- re-added identically. Wrapped in a transaction so the table is never briefly
-- unconstrained (an improvement on v108's unwrapped form).
--
-- NOT changed: the column TYPE. An old entity carries `character varying`, a fresh
-- entity `text`. They behave identically in Postgres and the rules that matter
-- — default and constraint — are what converge here; a type change would mean
-- a table rewrite for no behavioural gain.
BEGIN;

UPDATE projects
   SET status = 'planning',
       updated_at = now()
 WHERE status IS NULL
    OR status NOT IN ('planning','active','review','complete','archived');

ALTER TABLE projects ALTER COLUMN status SET DEFAULT 'planning';

ALTER TABLE projects DROP CONSTRAINT IF EXISTS projects_status_check;
ALTER TABLE projects ADD CONSTRAINT projects_status_check
    CHECK (status IN ('planning','active','review','complete','archived'));

COMMIT;
