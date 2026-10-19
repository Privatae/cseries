-- v174 (2026-09-23): an index for "this project's newest runs".
--
-- The Workbench's MODULES section reads a project's latest finished runs on
-- every bench poll (project_modules.run_candidates: WHERE project_id = $1
-- ORDER BY id DESC LIMIT 60). v079 indexed harness_run by session_id and
-- created_at only, so that read walked the whole table — which grows forever
-- (curation strips transcripts, never rows) — every few seconds while a bench
-- is open. Found in the Workbench Phase 3 review.
--
-- harness_run is created by migrations/v079, not init-db.sql, so this index
-- lives with the migrations too; fresh and long-lived entities both get it
-- from here.

CREATE INDEX IF NOT EXISTS idx_harness_run_project ON harness_run (project_id, id DESC);
