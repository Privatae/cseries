-- v215: what the Workbench's tabs read for each project.
--
-- 1. projects.seen_at — when the person last had the project's page open.
--    Set by PUT /api/projects/{id}/seen, which only ever moves it forward
--    (server/routes/projects.py mark_project_seen_service). A tab lights when
--    something the Host made lands after it. NULL = never marked.
--
-- 2. projects.approvals_waiting / approvals_checked_at — how many of the
--    project's Code Approvals were waiting for the person at the last read
--    of the forge, and when that read was. The forge is the record, and the
--    projects list cannot read it once per row, so it reads this copy, which
--    every forge read of the project's approvals writes
--    (server/routes/code_approvals.py record_waiting). NULL = never read.
--
-- None of the three is ever written together with updated_at: the list
-- orders by last activity, and marking a page seen is not activity.
--
-- Idempotent: ADD COLUMN IF NOT EXISTS, and the CHECK is dropped before it
-- is added. One transaction, so a boot stopped part way leaves none of it.

BEGIN;

ALTER TABLE projects ADD COLUMN IF NOT EXISTS seen_at TIMESTAMPTZ;
ALTER TABLE projects ADD COLUMN IF NOT EXISTS approvals_waiting INTEGER;
ALTER TABLE projects ADD COLUMN IF NOT EXISTS approvals_checked_at TIMESTAMPTZ;

ALTER TABLE projects DROP CONSTRAINT IF EXISTS projects_approvals_waiting_check;
ALTER TABLE projects ADD CONSTRAINT projects_approvals_waiting_check
    CHECK (approvals_waiting IS NULL OR approvals_waiting >= 0);

COMMIT;
