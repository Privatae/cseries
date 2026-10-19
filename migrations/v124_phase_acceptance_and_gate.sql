-- v124_phase_acceptance_and_gate.sql — phases prove themselves; gates are data (2026-09-03)
--
-- One Host's app project, 2026-09-03. Three phases were marked complete in
-- 27 minutes. Phase 2's brief asked to verify the app runs end-to-end; the
-- harness satisfied it by WRITING verify.sh and never ran a
-- thing. Phase 3's brief said "a live artifact sketch is produced and approved
-- by the user BEFORE this phase builds"; the auto-advance chain read only the
-- halt flag and the status column, so it built phase 3 anyway. The host then
-- reported a never-run static page as a live app.
--
-- Both defects were prose where there should have been a column. Until this
-- migration a phase flipped to 'complete' on harness exit 0 plus a manifest
-- check, and nothing in the schema could express "this phase is not buildable
-- until a person says so" or "this is what proves the phase did its job".
--
--   acceptance   JSONB   list of {cmd, expect_exit?, timeout_s?}. Run inside
--                        the harness pod, in the run's own worktree, AFTER the
--                        harness finishes (harness_core POST /check). A phase
--                        reaches 'complete' only when every command exits as
--                        expected. NULL/[] = nothing declared: the phase lands
--                        in 'review' and only the user can complete it.
--   gate         TEXT    'none' | 'user'. 'user' = not buildable until the user
--                        approves it from the Workbench; the auto-advance chain
--                        stops in front of it and says so. The host can SET the
--                        gate when it plans; no host tool can clear it.
--   approved_at  TIMESTAMPTZ  the user's approval (Workbench route only).
--
-- 'review' was already in the status CHECK (v108/v110) and is now written:
-- a phase that landed without executed acceptance.
--
-- Idempotent, forward-only.

ALTER TABLE project_phases ADD COLUMN IF NOT EXISTS acceptance JSONB;
ALTER TABLE project_phases ADD COLUMN IF NOT EXISTS gate TEXT NOT NULL DEFAULT 'none';
ALTER TABLE project_phases ADD COLUMN IF NOT EXISTS approved_at TIMESTAMPTZ;

ALTER TABLE project_phases DROP CONSTRAINT IF EXISTS project_phases_gate_check;
ALTER TABLE project_phases ADD CONSTRAINT project_phases_gate_check
    CHECK (gate IN ('none','user'));
