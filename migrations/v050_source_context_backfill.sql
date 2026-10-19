-- v050: Phase 2 source-driven threading — backfill source_context {kind,ref}
-- onto existing turn rows so the unified recall ladder
-- (mastercode.memory_engine.recall_recent_turns) can scope on it directly.
--
-- Idempotent + guarded: only touches turn rows that don't already carry the
-- descriptor (safe to re-run; safe under ON_ERROR_STOP startup). Derivation
-- mirrors mastercode.memory_engine.derive_source_context EXACTLY:
--
--   project_id set             -> {kind:project, ref:<project_id>}
--   metadata.app_id set        -> {kind:app,     ref:<app_id>}
--   source_session 'synaptive:'-> {kind:peer,    ref:<actor>}   (split on ':')
--   else (plain chat / circles)-> {kind:app,     ref:"circles"}
--
-- The else->circles mapping is deliberate and preserves prior behavior: an
-- untagged chat turn (project_id NULL, app_id NULL) used to surface in circles
-- recall via the old `metadata->>'app_id' IS NULL` OR-clause. The new ladder
-- removes that leak, so without this mapping those turns would silently drop
-- out of circles. Validated against a live Host: 125 project / 109
-- app:app-factory / 54 app:circles / 34 plain-chat(uuid,chat_turn) turn rows.
UPDATE entity_memory
SET metadata = jsonb_set(
    COALESCE(metadata, '{}'::jsonb),
    '{source_context}',
    CASE
        WHEN project_id IS NOT NULL
            THEN jsonb_build_object('kind', 'project', 'ref', project_id::text)
        WHEN metadata->>'app_id' IS NOT NULL
            THEN jsonb_build_object('kind', 'app', 'ref', metadata->>'app_id')
        WHEN source_session LIKE 'synaptive:%'
            THEN jsonb_build_object('kind', 'peer', 'ref', split_part(source_session, ':', 2))
        ELSE jsonb_build_object('kind', 'app', 'ref', 'circles')
    END,
    true
)
WHERE memory_type = 'turn'
  AND (metadata->'source_context') IS NULL;
