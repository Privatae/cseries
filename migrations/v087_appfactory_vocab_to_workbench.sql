-- Migration v087: rewrite the retired 'app-factory' vocabulary to 'workbench'
-- in STORED rows, so the code-side compat branches can be deleted.
--
-- AppFactory was deprecated 2026-07-01 — the
-- Workbench absorbed its role. The rename left three live compat branches
-- (prompt_builder._classify_surface, tools._resolve_surface, the kernel
-- RETIRED_APPS sweep) whose only reason to exist was data written BEFORE the
-- rename still carrying the old id (validated on one Host at v050 time: 109
-- app:app-factory turn rows). This migration rewrites that data; the same
-- change deletes the branches. chat_events has no app_id column and never
-- carried the id — nothing to rewrite there.
--
-- Idempotent UPDATEs (each WHERE only matches old-vocab rows), single
-- transaction, safe under ON_ERROR_STOP on every pod generation.

BEGIN;

-- 1. Sessions that were opened under the AppFactory surface. The surface
--    classifier maps 'workbench' identically to what the compat branch did.
UPDATE entity_sessions
   SET interaction_context = 'workbench'
 WHERE interaction_context = 'app-factory';

-- 2. Turn rows tagged with the old app id (write_turn_row metadata.app_id).
UPDATE entity_memory
   SET metadata = jsonb_set(metadata, '{app_id}', '"workbench"', false)
 WHERE metadata->>'app_id' = 'app-factory';

-- 3. Source-context descriptors derived from the old id ({kind:'app',
--    ref:'app-factory'} — the shape v050 backfilled). Project-scoped rows
--    ({kind:'project'}) never carried the app id and are untouched.
UPDATE entity_memory
   SET metadata = jsonb_set(metadata, '{source_context,ref}', '"workbench"', false)
 WHERE metadata->'source_context'->>'kind' = 'app'
   AND metadata->'source_context'->>'ref' = 'app-factory';

-- 4. Retire any lingering installed copy of the app itself. This replaces the
--    kernel boot sweep's RETIRED_APPS 'app-factory' entry (deleted in the same
--    change): the sweep re-ran the uninstall every boot forever; one row
--    delete finishes the retirement. Config rows/workspace survive orphaned —
--    the same fail-soft contract the sweep had.
DELETE FROM installed_applications WHERE id = 'app-factory';

COMMIT;
