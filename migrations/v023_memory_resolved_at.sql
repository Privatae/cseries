-- Migration v023: add resolved_at column for goal/unresolved memory lifecycle.
--
-- Adds entity_memory.resolved_at — when a goal/unresolved memory is marked
-- as completed/answered. Used by:
--   * synaptive_handler recent-active loader: include goal/unresolved
--     in prompt context ONLY when resolved_at IS NULL AND still fresh.
--   * memory_consolidation._decay_old: drop importance faster on
--     resolved rows so they fall out of retrieval rotation.
--
-- Idempotent. Existing rows have resolved_at = NULL (treated as
-- unresolved — fresh ones still appear in recent-active, stale ones
-- decay normally).

BEGIN;

ALTER TABLE entity_memory
    ADD COLUMN IF NOT EXISTS resolved_at TIMESTAMPTZ;

-- Index supports the recent-active loader's filter pattern
-- (status='active' + resolved_at IS NULL is the hot lookup for goal/unresolved).
CREATE INDEX IF NOT EXISTS idx_entity_memory_resolved_at_null
    ON entity_memory (entity_id, memory_type, created_at DESC)
    WHERE status = 'active' AND resolved_at IS NULL
      AND memory_type IN ('goal','unresolved');

COMMIT;
