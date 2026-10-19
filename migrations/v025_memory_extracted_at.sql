-- Migration v025: add extracted_at column for turn-row consolidation lifecycle.
--
-- Adds entity_memory.extracted_at — when the cron consolidation worker has
-- processed a memory_type='turn' row through extract_and_store +
-- consolidate_new. Only meaningful for turn rows; other types stay NULL.
--
-- Used by:
--   * cron worker: SELECT WHERE memory_type='turn' AND extracted_at IS NULL
--     to find pending work.
--   * extract_and_store: stamps extracted_at = now() after the typed rows
--     it produced have been classified through consolidate_new.
--
-- Why: Ship 4a writes every primary-chat turn as a turn row in real time
-- with no LLM cost in the hot path. The extract+consolidate cost moves to
-- this cron-driven batched path. extracted_at is the queue marker.
--
-- Idempotent. Existing rows have extracted_at = NULL — the cron will
-- pick them up on first run unless we backfill (we DON'T backfill: only
-- new turn writes from session.py post-deploy should flow through, and
-- the synaptive verbatim path's existing 'turn' rows from Ship 1 are
-- bench/external-signal in origin, not chat — leave them alone).

BEGIN;

ALTER TABLE entity_memory
    ADD COLUMN IF NOT EXISTS extracted_at TIMESTAMPTZ;

-- Partial index for the cron's hot lookup: unprocessed turn rows, oldest first.
CREATE INDEX IF NOT EXISTS idx_entity_memory_turn_unprocessed
    ON entity_memory (entity_id, created_at ASC)
    WHERE memory_type = 'turn' AND extracted_at IS NULL;

COMMIT;
