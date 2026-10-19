-- v189 (2026-09-27): a Circles thread can be DELETED, not only closed.
--
-- Clearing a conversation archives its turn rows and nothing else
-- (memory_engine._turn_row_where: memory_type = 'turn'), so a thread that went
-- badly kept steering its Host: the summaries, goals and facts consolidation
-- derived from it stay active, and open goals are read by every thread
-- (batch_consolidator._open_goals is not thread-scoped). That makes a Host hard
-- to test when poor answers corrupt its memory.
--
-- Deleting a thread (circles_threads.delete_thread) archives its turn rows AND
-- every row learned inside it, undoes what it retired elsewhere, and stamps
-- this column. The row stays — archived, never erased — and every read of the
-- thread list, one thread, or a send into it treats a deleted thread as absent.
ALTER TABLE circles_threads ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;

-- The delete reads chains of replacements backwards (circles_threads._undo:
-- every row whose resolution_pointer leads into what the delete archived), and
-- nothing indexed that column: measured 16s for a delete on an entity with
-- 124,750 memories. Partial: most rows point nowhere.
CREATE INDEX IF NOT EXISTS idx_entity_memory_resolution_pointer
    ON entity_memory (resolution_pointer) WHERE resolution_pointer IS NOT NULL;

-- And the delete asks, for every row it would bring back, whether the same
-- words are already live (circles_threads._live_twin: one live copy). Only
-- (entity_id, memory_type) was indexed, so each ask scanned every row of that
-- type: 11 of those 16 seconds. A hash index answers word-for-word equality
-- directly; the duplicate guards that match on content use it too.
CREATE INDEX IF NOT EXISTS idx_entity_memory_content_hash
    ON entity_memory USING hash (content);
