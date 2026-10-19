-- Migration v037: grant every existing entity read_write on device.filesystem.
--
-- v036 added the synapse from core.entity → device.filesystem. Ship 6
-- verification then surfaced the next layer: the entity also needs a
-- row in entity_domain_access for device.filesystem. An existing pod had
-- ZERO rows for that domain — the controller defaulted to "none" and
-- denied every ceigas_write with "insufficient access (have none,
-- need read_write)".
--
-- init-db.sql:852 seeds 8 default domains for fresh entities but
-- never included device.filesystem. v037 fills that gap.
--
-- Defense in depth: this is layer 2 of 4. The full stack:
--   1. Synapse (v036)       — does the crossing exist at all?
--   2. Access level (v037)  — does the entity have read_write?
--   3. policy.py path scope — is the path under /var/lib/cseries/projects/**?
--   4. sdk_build cwd jail   — is the build target inside the project_id dir?
-- All four must pass for a file to land.
--
-- Idempotent: WHERE NOT EXISTS guard so reruns are no-ops.

BEGIN;

INSERT INTO entity_domain_access
    (entity_id, domain_id, access_level, granted_by, grant_reason)
SELECT
    er.id, 'device.filesystem', 'read_write',
    'v037_migration',
    'Entity needs filesystem read/write for sdk_build, deep_research, '
    'and any CEIGAS-gated MCP write. Scoped by policy.py paths + '
    'sdk_build cwd jail to /var/lib/cseries/projects/<own_id>/ only.'
FROM entity_registry er
WHERE NOT EXISTS (
    SELECT 1 FROM entity_domain_access eda
    WHERE eda.entity_id = er.id AND eda.domain_id = 'device.filesystem'
);

COMMIT;
