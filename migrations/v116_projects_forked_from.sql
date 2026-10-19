-- v116_projects_forked_from.sql
-- (v112 is v112_harness_usage_phase_id.sql: the entity migration loop keys
-- its ledger on the NUMBER alone, so a duplicate number is silently skipped
-- on every existing entity.)
--
-- The provenance ratchet, entity side.
-- When a project is created by FORKING an
-- installed store app (the fork-to-project flow), the
-- creator records {"app_id": ..., "version": ...} here AT FORK TIME. The
-- publish lane (publish_project_service) reads this column and submits it
-- as the flavor's parent edge — provenance is captured when the fork
-- happens, never reconstructed or trusted at publish time.
--
-- Ships BEFORE the fork flow exists, on purpose: history only exists if
-- recorded from the start. Every existing project is NULL (not a fork) —
-- correct by construction, since no fork operation predates this column.
-- Companion declaration for fresh entities: init-db.sql projects DDL.

ALTER TABLE projects
    ADD COLUMN IF NOT EXISTS forked_from JSONB;
