-- v235 — one memory, the platform's part.
--
-- WHAT THIS DOES, once, on every Host:
--   1. The one-time effects of the retired boot pass
--      (memory_consolidation.archive_retired_memory / retype_rules, deleted
--      with this migration): taught and learned skill_memory rows become
--      preference rules; the retired row classes are archived.
--   2. Rows of a memory type that no longer exists are archived and retyped
--      'retired', keeping the old name in metadata.prior_memory_type, so the
--      CHECK can narrow to the kept types and the Memory app's as-of view
--      still shows what was there.
--   3. The ten-dimension personality vector and its companions leave
--      entity_memory, with the consolidator queue markers and the unused
--      episode and role columns.
--   4. The simulated psyche's tables, the batch consolidator's ledger and the
--      memory-network scaffolding are dropped (codebook_snapshots stays until
--      the coding-bench cluster that references it goes). The AGE graph goes with its
--      extension: its only writer (graph extraction in the consolidator loop)
--      is off, and the recall arm it fed carried no ranking weight
--      (memory_retrieval, GRAPH_RRF_WEIGHT 0, measured).
-- Idempotent: every statement is IF EXISTS / WHERE-guarded. Forward-only.

BEGIN;

-- ── 1. the retired boot pass, once ──────────────────────────────────────────
-- The taught triggers are memory_engine._TEACHING_PATTERNS' labels.
UPDATE entity_memory SET memory_type = 'preference',
    metadata = COALESCE(metadata, '{}'::jsonb) || jsonb_build_object(
      'rule_kind', COALESCE(metadata->>'rule_kind', CASE
        WHEN metadata->>'trigger' = 'entity_learned'
          OR metadata->>'source' = 'entity_learned' THEN 'learned'
        ELSE 'taught' END),
      'prior_memory_type', 'skill_memory',
      'retyped_at', to_jsonb(NOW())),
    updated_at = NOW()
 WHERE memory_type = 'skill_memory' AND (
    COALESCE(metadata ? 'rule_kind', false)
    OR (status = 'active' AND (
      metadata->>'source' = 'entity_learned'
      OR metadata->>'trigger' = 'entity_learned'
      OR metadata->>'trigger' IN ('remember this:', 'always remember:', 'never forget:',
                                  'learn this:', 'important: remember', 'teaching mode:'))));

UPDATE entity_memory SET status = 'archived',
    status_reason = 'memory integrity: constitutional values pack (values, not the platform''s operating rules)',
    status_changed_at = NOW()
 WHERE status = 'active' AND memory_type = 'constitutional' AND (
    metadata->>'origin' = 'constitutional-pack-v1'
    OR metadata ? 'principle_id'
    OR metadata->>'category' IN ('principle', 'human_understanding'));

UPDATE entity_memory SET status = 'archived',
    status_reason = 'memory integrity: open goal the consolidator wrote (goals come from close_turn)',
    status_changed_at = NOW()
 WHERE status = 'active' AND memory_type = 'goal' AND resolved_at IS NULL
   AND COALESCE(metadata->>'written_by', '') <> 'close_turn'
   AND (metadata ? 'emotion_source' OR metadata ? 'from_turns');

-- ── 2. retired types ────────────────────────────────────────────────────────
-- The old CHECK does not allow 'retired', so it goes BEFORE the retype and the
-- new one is added after it. (In the other order the retype fails on every Host
-- that holds a row of a retired type; a fresh database holds none, which is why
-- only a database that already held such rows showed it.)
ALTER TABLE entity_memory DROP CONSTRAINT IF EXISTS entity_memory_memory_type_check;
UPDATE entity_memory SET
    status = CASE WHEN status = 'active' THEN 'archived' ELSE status END,
    status_reason = CASE WHEN status = 'active'
                         THEN 'type retired 2026-10'
                         ELSE status_reason END,
    status_changed_at = CASE WHEN status = 'active' THEN NOW() ELSE status_changed_at END,
    metadata = COALESCE(metadata, '{}'::jsonb)
               || jsonb_build_object('prior_memory_type', memory_type),
    memory_type = 'retired',
    updated_at = NOW()
 WHERE memory_type IN ('fact', 'decision', 'topic', 'emotion', 'unresolved',
                       'knowledge', 'skill_memory', 'moral_precedent', 'legacy');

ALTER TABLE entity_memory ADD CONSTRAINT entity_memory_memory_type_check
    CHECK (memory_type IN ('turn', 'summary', 'preference', 'relationship',
                           'constitutional', 'goal', 'cited_fact', 'research',
                           'governance_observation', 'retired'));

-- ── 3. columns ──────────────────────────────────────────────────────────────
DROP INDEX IF EXISTS idx_entity_memory_graph_queue;
DROP INDEX IF EXISTS idx_entity_memory_teaching;
DROP INDEX IF EXISTS idx_entity_memory_turn_unprocessed;
DROP INDEX IF EXISTS idx_entity_memory_emotion_tag;
DROP INDEX IF EXISTS idx_memory_episode;
DROP INDEX IF EXISTS idx_entity_memory_role;
DROP INDEX IF EXISTS idx_entity_memory_dimension_scores;
DROP INDEX IF EXISTS idx_entity_memory_dimvec_hnsw;
DROP INDEX IF EXISTS idx_entity_memory_vector;
ALTER TABLE entity_memory
    DROP COLUMN IF EXISTS dimension_vector,
    DROP COLUMN IF EXISTS dimension_scores,
    DROP COLUMN IF EXISTS episode_id,
    DROP COLUMN IF EXISTS role,
    DROP COLUMN IF EXISTS extracted_at,
    DROP COLUMN IF EXISTS graph_extracted_at;

-- ── 4. tables and the graph ─────────────────────────────────────────────────
ALTER TABLE entity_registry DROP CONSTRAINT IF EXISTS entity_registry_mastercode_version_fkey;
ALTER TABLE entity_registry DROP COLUMN IF EXISTS mastercode_version;
DROP TABLE IF EXISTS mastercode_book CASCADE;
DROP TABLE IF EXISTS codebook_signal_log CASCADE;
DROP TABLE IF EXISTS codebook_params CASCADE;
DROP TABLE IF EXISTS codebook_registry CASCADE;
DROP TABLE IF EXISTS neurotransmitter_systems CASCADE;
DROP TABLE IF EXISTS entity_interactions CASCADE;
DROP TABLE IF EXISTS entity_episodes CASCADE;
DROP TABLE IF EXISTS memory_pass_log CASCADE;
DROP TABLE IF EXISTS memory_subscriptions CASCADE;
DROP TABLE IF EXISTS platform_audit_chain CASCADE;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'age') THEN
        EXECUTE 'DROP EXTENSION age CASCADE';
    END IF;
    -- AGE leaves its catalog schema and the graph's schema behind, empty.
    EXECUTE 'DROP SCHEMA IF EXISTS cseries_world CASCADE';
    EXECUTE 'DROP SCHEMA IF EXISTS ag_catalog CASCADE';
END $$;

COMMIT;
