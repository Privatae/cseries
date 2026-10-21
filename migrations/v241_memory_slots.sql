-- v241 — memory slots: one live entry per (type, scope, topic).
--
-- WHAT THIS DOES, once, on every Host:
--   1. entity_memory gains three columns:
--        slot_topic  what the entry is about, as the Host named it in its own
--                    memory call, normalized in code to [a-z0-9_], at most 48
--                    characters (brain/mastercode/memory_slots.py, normalize).
--        slot_scope  where the entry is read: 'global', or '<kind>:<ref>' for a
--                    project, an app or a thread. Set by the platform from the
--                    entry's scope decision (metadata.about), else where it was
--                    said (metadata.source_context), else 'global'. A
--                    relationship note, and a rule nobody has scoped, are read
--                    everywhere and so are 'global'.
--        turn_type   the name of the turn type the writing turn ran under
--                    ('untyped' for a turn with none). Stamped by the platform,
--                    never chosen by the model. Provenance only: it is not part
--                    of the slot, so a correction made in a turn of another
--                    type still replaces the older entry.
--   2. A unique partial index makes one LIVE entry per slot a database rule.
--      A write to an occupied slot supersedes the older entry in the same
--      transaction (memory_slots.write_in_slot); a second live row in a slot
--      fails loudly instead of riding beside the first.
--   3. Existing rows: slot_scope is filled for preferences, relationship notes
--      and goals by the same rule the writers use. slot_topic stays NULL on
--      every existing row: no model guesses a topic. A row with no topic is
--      outside the slot rule and keeps today's behaviour until the Host (or the
--      owner, in the Memory app) gives it one.
--   4. circles_threads.merged_into: a topic made for one message and then
--      folded into the thread the Host picked for it in its own turn
--      (runtime/thread_choice.py) names where it went ('<kind>:<ref>'), so a
--      page that still names it sends there (circles_threads.live_thread_id).
--   5. Live rolling-summary rows are archived. The summary writer (a separate
--      model call outside the Host's turn) is gone; the Memory app's as-of view
--      keeps what was there.
-- Idempotent: every statement is IF NOT EXISTS or WHERE-guarded. Forward-only.

BEGIN;

ALTER TABLE entity_memory ADD COLUMN IF NOT EXISTS slot_topic TEXT;
ALTER TABLE entity_memory ADD COLUMN IF NOT EXISTS slot_scope TEXT;
ALTER TABLE entity_memory ADD COLUMN IF NOT EXISTS turn_type  TEXT;

-- A topic is the normalized form only, and a topic always has a scope.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'entity_memory_slot_topic_shape'
                      AND conrelid = 'entity_memory'::regclass) THEN
        ALTER TABLE entity_memory ADD CONSTRAINT entity_memory_slot_topic_shape
            CHECK (slot_topic IS NULL
                   OR (slot_topic ~ '^[a-z0-9][a-z0-9_]{0,47}$' AND slot_scope IS NOT NULL));
    END IF;
END $$;

-- The scope of every existing preference, relationship note and goal, by the
-- writers' own rule (memory_slots.slot_scope_for).
UPDATE entity_memory
   SET slot_scope = CASE
         WHEN memory_type = 'relationship' THEN 'global'
         WHEN jsonb_typeof(metadata->'about') = 'object'
              AND COALESCE(metadata->'about'->>'kind', '') NOT IN ('', 'global')
              THEN (metadata->'about'->>'kind') || ':' || COALESCE(metadata->'about'->>'ref', '')
         WHEN jsonb_typeof(metadata->'about') = 'object' THEN 'global'
         WHEN metadata ? 'rule_kind' THEN 'global'
         WHEN jsonb_typeof(metadata->'source_context') = 'object'
              AND COALESCE(metadata->'source_context'->>'kind', '') NOT IN ('', 'global')
              THEN (metadata->'source_context'->>'kind') || ':'
                   || COALESCE(metadata->'source_context'->>'ref', '')
         ELSE 'global'
       END
 WHERE slot_scope IS NULL
   AND memory_type IN ('preference', 'relationship', 'goal');

CREATE UNIQUE INDEX IF NOT EXISTS ux_entity_memory_live_slot
    ON entity_memory (entity_id, memory_type, slot_scope, slot_topic)
 WHERE slot_topic IS NOT NULL AND status = 'active' AND t_valid_until IS NULL;

-- Recall without an embedder matches a row by its topic as well as its words
-- (memory_retrieval._lexical_arm).
CREATE INDEX IF NOT EXISTS idx_entity_memory_slot_topic_tsv
    ON entity_memory USING gin (to_tsvector('english'::regconfig, replace(slot_topic, '_', ' ')))
 WHERE slot_topic IS NOT NULL;

ALTER TABLE circles_threads ADD COLUMN IF NOT EXISTS merged_into TEXT;

-- The summary writer is retired: its live rows leave the prompt and stay in
-- the as-of view.
UPDATE entity_memory
   SET status = 'archived', status_reason = 'summary writer retired',
       status_changed_at = now(), updated_at = now()
 WHERE memory_type = 'summary' AND status = 'active' AND t_valid_until IS NULL;

COMMIT;
