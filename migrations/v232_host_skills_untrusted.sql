-- v232: a Host skill written in a turn that read untrusted
-- content carries that mark, as every other row such a turn writes does.
--
-- A turn that reads content from outside (the web, a foreign repository, an
-- MCP tool's output, another Host's message, an agent's report, an upload) is
-- marked for the rest of the turn (runtime/turn_taint.py). A memory row, a
-- turn row or a scheduled task written while the turn is marked carries
-- `untrusted` in its metadata (turn_taint.stamp), and a later turn that reads
-- it back is marked too (turn_taint.mark_rows). A skill had no such mark: a
-- skill the Host wrote right after reading a web page reached every later
-- turn that read it (recall's skills store, the per-turn offer of the Host's
-- own skills, a context read of skills/<name>, a turn type riding it) with
-- nothing saying what was in its turn. That is a way for instructions planted
-- in outside content to persist, and Hosts write their own skills only where
-- such instructions cannot get through.
--
--   untrusted   what marked the turn the version was written in (the same
--               words turn_taint.stamp writes into a row's metadata); NULL
--               for a version written in a clean turn, by the person in the
--               Skills app (they read what they saved), or before this
--               migration. runtime/host_skills.write sets it; a turn that
--               reads a version carrying it is marked (turn_taint.mark_rows).
--
-- Idempotent (IF NOT EXISTS). host_skills is created by v207 only, so there
-- is no init-db.sql declaration to keep in step.

BEGIN;

ALTER TABLE host_skills ADD COLUMN IF NOT EXISTS untrusted TEXT;

COMMENT ON COLUMN host_skills.untrusted IS
    'What marked the turn this version was written in (runtime/turn_taint.stamp); NULL when clean or written by the person.';

COMMIT;
