-- v225: the Host's ROOT thread (runtime/root_turn.py).
--
-- Root on a machine runs in a turn type of its own, and that
-- turn is its own thread in the conversation. The root type is built in (its
-- tools and context are a fixed allowlist in code, never a row the person or
-- the Host edits), so its thread is marked here rather than bound through
-- turn_types: `builtin_type = 'root'`. A thread with NULL is every thread as
-- before. One live root thread per Host; a deleted one leaves room for the
-- next, which the Host opens again from any thread.
--
-- Numbered 225: v222 (pending_words), v223 and v224 are taken. Mirrored in
-- init-db.sql.

BEGIN;

ALTER TABLE circles_threads
    ADD COLUMN IF NOT EXISTS builtin_type TEXT NULL;

ALTER TABLE circles_threads DROP CONSTRAINT IF EXISTS circles_threads_builtin_type_check;
ALTER TABLE circles_threads ADD CONSTRAINT circles_threads_builtin_type_check
    CHECK (builtin_type IS NULL OR builtin_type IN ('root'));

CREATE UNIQUE INDEX IF NOT EXISTS uq_circles_threads_live_builtin
    ON circles_threads (entity_id, builtin_type)
    WHERE builtin_type IS NOT NULL AND deleted_at IS NULL;

COMMIT;
