-- v081: repo-per-project (the Workbench is the AI
-- interface for a repo; every project gets its own gitea repo at creation).
-- projects.repo holds the gitea full_name (owner/name); NULL = created
-- before this ship or gitea wasn't connected at creation time (fail-soft —
-- ensure_project_repo backfills on next create attempt).
ALTER TABLE projects ADD COLUMN IF NOT EXISTS repo TEXT;
