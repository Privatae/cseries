-- Migration v036: seed the missing core.entity → device.filesystem synapse.
--
-- Discovered during Ship 6 verification: the entity's CEIGAS Domain
-- Controller denies every ceigas_write call with "no synapse from
-- core.entity to device.filesystem (conductance 0.0)". The existing
-- synapse seeds in init-db.sql:721-780 connect core.entity to
-- bots.execution / core.cognitive / core.memory / etc. but not to
-- device.filesystem — only container.relay has that crossing.
--
-- This means the entity (acting from core.entity) cannot write files
-- to the persistent volume via the CEIGAS-gated MCP write tool —
-- breaking sdk_build, deep_research's file recording, and any future
-- file-touching meta-tool.
--
-- Per Invariant 4 (Synapse Classification), this crossing
-- should exist as a `gated` synapse (requires explicit permission
-- check + conductance modulation). The entity has admin access on
-- core.entity domain, and the policy.py rules already restrict the
-- actual filesystem paths to /var/lib/cseries/projects/** + workspaces.
-- The synapse layer is the OUTER gate; policy + jail are inner gates.
-- Defense in depth.
--
-- Additive + idempotent. ON CONFLICT DO NOTHING so reruns are safe.

BEGIN;

INSERT INTO domain_synapse
    (source_domain, target_domain, synapse_type, base_conductance,
     nt_modulation, rate_limit_rpm, description, enabled)
VALUES (
    'core.entity', 'device.filesystem', 'gated',
    1.0,
    '{}'::jsonb,
    0,
    'Entity writes/reads files via CEIGAS MCP (ceigas_read/write/edit). '
    'Gated by policy.py path scopes (/var/lib/cseries/projects/**) + the '
    'local_grep / sdk_build cwd jail. Added Ship 6 (2026-05-24) — the '
    'crossing was needed by sdk_build but never seeded in init-db.sql.',
    true
)
ON CONFLICT DO NOTHING;

-- Add a unique constraint on (source_domain, target_domain) so
-- duplicate seeds via this or any future migration are no-ops. Skip
-- if the constraint already exists (some pods may already have it
-- under a different name).
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
         WHERE tablename = 'domain_synapse'
           AND indexname = 'uq_domain_synapse_source_target'
    ) THEN
        CREATE UNIQUE INDEX uq_domain_synapse_source_target
            ON domain_synapse (source_domain, target_domain);
    END IF;
END $$;

COMMIT;
