-- Migration v038: seed missing synapses + entity_domain_access for the
-- synaptive MCP CEIGAS gate (Ship 7).
--
-- Ship 5's CEIGAS audit gap #4: the /mcp route handler bypasses every
-- invariant. Ship 7 adds a route-level gate that runs
-- authorize_crossing per call. The gate's source/target domains
-- depend on the caller and the tool:
--
--   internal (SDK shim, INTERNAL_API_SECRET) → core.entity → <target>
--   external (bearer token MCP client)       → external.mcp  → <target>
--
-- Where <target> per tool:
--   shape / recall / signal  → synaptive.shape
--   open_app / close_app / list_apps → core.entity
--
-- Domain registry state verified on an existing pod:
--   ✓  synaptive.shape registered
--   ✓  external.mcp registered
--   ✓  synapse external.mcp → synaptive.shape exists
--   ✗  synapse external.mcp → core.entity MISSING
--   ✗  synapse core.entity → synaptive.shape MISSING
--   ✗  the entity has no entity_domain_access row for external.mcp
--   ✗  the entity has no entity_domain_access row for synaptive.shape
--
-- v038 fills all four gaps.
--
-- Additive + idempotent. ON CONFLICT DO NOTHING + WHERE NOT EXISTS so
-- reruns are safe across both fresh init-db.sql pods and drifted pods.

BEGIN;

-- ── Missing synapses ──────────────────────────────────────────
-- Synapse uniqueness was enforced in v036 (uq_domain_synapse_source_target);
-- both inserts are no-ops if a row already exists for that pair.

INSERT INTO domain_synapse
    (source_domain, target_domain, synapse_type, base_conductance,
     nt_modulation, rate_limit_rpm, description, enabled)
VALUES
    ('external.mcp', 'core.entity', 'gated',
     1.0, '{}'::jsonb, 0,
     'External MCP client → entity app/state ops (open_app, close_app, '
     'list_apps). Added Ship 7 (2026-05-24).',
     true),
    ('core.entity', 'synaptive.shape', 'gated',
     1.0, '{}'::jsonb, 0,
     'Internal SDK shim → synaptive shape/recall/signal. Added Ship 7 '
     '(2026-05-24). Pairs with the existing external.mcp → '
     'synaptive.shape synapse for the bearer-token caller surface.',
     true)
ON CONFLICT DO NOTHING;


-- ── Missing entity_domain_access rows ─────────────────────────
-- Every entity gets read_write on external.mcp (so external MCP
-- clients with proper bearer tokens can call synaptive tools) and
-- admin on synaptive.shape (the entity owns its
-- OWN CEIGAS domain, including its shape).

INSERT INTO entity_domain_access
    (entity_id, domain_id, access_level, granted_by, grant_reason)
SELECT
    er.id, 'external.mcp', 'read_write',
    'v038_migration',
    'Entity needs external.mcp read_write so dashboard / SDK subagent / '
    'third-party MCP clients can call synaptive tools. Defense in '
    'depth: the /mcp route gate (Ship 7) + the invariant in '
    'CEIGASDomainController + the audit row. Token-bound scopes '
    '(shape/apps/factory) are the inner gate inside the route.'
FROM entity_registry er
WHERE NOT EXISTS (
    SELECT 1 FROM entity_domain_access eda
    WHERE eda.entity_id = er.id AND eda.domain_id = 'external.mcp'
);

INSERT INTO entity_domain_access
    (entity_id, domain_id, access_level, granted_by, grant_reason)
SELECT
    er.id, 'synaptive.shape', 'admin',
    'v038_migration',
    'Entity is admin of its own synaptive shape (personality / avatar / '
    'recall / signal). Each entity is admin of its OWN '
    'CEIGAS domain — shape/recall/signal write into the entity own '
    'substrate. read_write isn''t strong enough because the entity may '
    'mutate its own personality weights / journal entries.'
FROM entity_registry er
WHERE NOT EXISTS (
    SELECT 1 FROM entity_domain_access eda
    WHERE eda.entity_id = er.id AND eda.domain_id = 'synaptive.shape'
);

COMMIT;
