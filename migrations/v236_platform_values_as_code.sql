-- v236 — the platform's values are code; a Host's own rules are preferences.
--
-- The five platform values (v094, entity 0) render from
-- brain/mastercode/constitution/platform_values.py now, so their rows are
-- archived here. A `constitutional` row a Host holds for itself was either the
-- genesis copy of a platform pack (archived by v235's values-pack rule) or a
-- rule written for that Host; the latter becomes a preference marked rule_kind
-- 'taught', the shape every rule reader already handles, and rides where the
-- memory block puts it. Idempotent; forward-only.

BEGIN;

UPDATE entity_memory SET status = 'archived',
    status_reason = 'platform values are code (constitution/platform_values.py)',
    status_changed_at = NOW(), updated_at = NOW()
 WHERE status = 'active' AND memory_type = 'constitutional' AND entity_id = 0;

UPDATE entity_memory SET memory_type = 'preference',
    metadata = COALESCE(metadata, '{}'::jsonb) || jsonb_build_object(
        'rule_kind', COALESCE(metadata->>'rule_kind', 'taught'),
        'prior_memory_type', 'constitutional',
        'retyped_at', to_jsonb(NOW())),
    updated_at = NOW()
 WHERE status = 'active' AND memory_type = 'constitutional' AND entity_id <> 0;

COMMIT;
