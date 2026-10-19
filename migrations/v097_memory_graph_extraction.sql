-- v097: graph-extraction queue marker.
--
-- The associative layer extracts entities/relations from memories into the
-- per-entity AGE graph on the consolidation cron. graph_extracted_at tracks
-- the queue: NULL = not yet extracted. New memories and the EXISTING-row
-- backfill (relevant existing data must not be abandoned)
-- drain through the same relevance-ordered queue — importance first, bounded
-- per pass — so the backlog empties over cycles with no separate job.
--
-- Turn rows are excluded from the queue (raw conversation; their substance
-- reaches the graph via the facts consolidation derives from them).
-- A failed extraction leaves NULL (retried next pass); an empty extraction
-- still stamps (processed ≠ pending).

BEGIN;

ALTER TABLE entity_memory ADD COLUMN IF NOT EXISTS graph_extracted_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_entity_memory_graph_queue
    ON entity_memory (importance DESC)
    WHERE graph_extracted_at IS NULL AND status = 'active'
      AND t_valid_until IS NULL AND memory_type != 'turn';

COMMIT;
