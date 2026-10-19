-- v128: the commit↔run link's reverse index.
--
-- A host save-point commit
-- carries the numbered coding runs whose files it holds as `Host-Run: <id>`
-- git trailers (routes/project_git.py commit_message); after the push the
-- same link is written the other way onto the run rows —
-- harness_run.metadata->>'commit_sha' (routes/compute.py link_runs_to_commit).
-- This index is what makes "which run produced commit X" one lookup for the
-- store listing and the attestation. The writer's _ensure_schema carries the
-- same statement (belt-and-suspenders, like v079's table); this file is the
-- canonical add. Idempotent.

CREATE INDEX IF NOT EXISTS idx_harness_run_commit
    ON harness_run ((metadata->>'commit_sha'));
