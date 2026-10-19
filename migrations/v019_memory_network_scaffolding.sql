-- v019_memory_network_scaffolding.sql
--
-- Ship A catch-up: add to existing entity pods the same schema fresh
-- pods now get via init-db.sql. Idempotent (CREATE IF NOT EXISTS,
-- ADD COLUMN IF NOT EXISTS). Safe to re-run.
--
-- What this lands:
--   1. age extension (timescaledb removed 2026-07-19 — see note at the
--      CREATE EXTENSION block below)
--   2. Bi-temporal columns on entity_memory (t_valid_from, t_valid_until,
--      resolution_pointer) + backfill from created_at
--   3. The CEIGAS domain registry (domain_registry, domain_synapse,
--      domain_signal_log, trust_ledger, entity_domain_access) + seeds
--   4. Memory-network protocol tables (platform_audit_chain,
--      memory_subscriptions, config_versions)
--   5. Per-entity self-access grants (every entity needs execute on
--      core.cognitive or chat denies "insufficient access")
--
-- Sister to init-db.sql; they stay aligned. When init-db.sql changes
-- substantively for existing tables this migration backfills the gap.

-- timescaledb was created here until 2026-07-19 (density pass): it carried
-- zero hypertables fleet-wide and cost ~27MB of background workers per pod.
-- Existing pods drop it via the entrypoint's one-time removal path; fresh
-- pods must never install it (this migration runs on first boot, where the
-- new postgresql.conf no longer preloads the library and the CREATE would
-- abort the boot).
CREATE EXTENSION IF NOT EXISTS age;

-- ── Bi-temporal validity on entity_memory ─────────────────────────────
ALTER TABLE entity_memory
    ADD COLUMN IF NOT EXISTS t_valid_from       TIMESTAMPTZ;
ALTER TABLE entity_memory
    ADD COLUMN IF NOT EXISTS t_valid_until      TIMESTAMPTZ;
ALTER TABLE entity_memory
    ADD COLUMN IF NOT EXISTS resolution_pointer INTEGER REFERENCES entity_memory(id);

-- Backfill: every existing memory has been valid since it was created
-- and hasn't been superseded (until consolidation says otherwise).
UPDATE entity_memory
   SET t_valid_from = COALESCE(t_valid_from, created_at, NOW())
 WHERE t_valid_from IS NULL;

-- Now enforce NOT NULL on t_valid_from.
ALTER TABLE entity_memory
    ALTER COLUMN t_valid_from SET NOT NULL;
ALTER TABLE entity_memory
    ALTER COLUMN t_valid_from SET DEFAULT now();

CREATE INDEX IF NOT EXISTS idx_entity_memory_live
    ON entity_memory (entity_id, t_valid_from DESC)
    WHERE t_valid_until IS NULL;

-- ── CEIGAS domain registry (was migrations/v018, restated here for any
--    pod that missed v018) ────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS domain_registry (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    brain_analog TEXT,
    description TEXT,
    domain_group TEXT NOT NULL,
    services JSONB DEFAULT '[]'::jsonb,
    ports JSONB DEFAULT '[]'::jsonb,
    health_probe TEXT,
    tier_access_defaults JSONB DEFAULT '{}'::jsonb,
    nt_sensitivity JSONB DEFAULT '{}'::jsonb,
    status TEXT DEFAULT 'active'
        CHECK (status IN ('active', 'degraded', 'offline', 'maintenance')),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS domain_synapse (
    id SERIAL PRIMARY KEY,
    source_domain TEXT NOT NULL REFERENCES domain_registry(id),
    target_domain TEXT NOT NULL REFERENCES domain_registry(id),
    synapse_type TEXT NOT NULL
        CHECK (synapse_type IN ('direct', 'gated', 'modulated', 'restricted', 'federated')),
    base_conductance FLOAT DEFAULT 1.0
        CHECK (base_conductance >= 0 AND base_conductance <= 1.0),
    nt_modulation JSONB DEFAULT '{}'::jsonb,
    rate_limit_rpm INTEGER DEFAULT 0,
    description TEXT,
    enabled BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(source_domain, target_domain)
);

CREATE TABLE IF NOT EXISTS domain_signal_log (
    id BIGSERIAL PRIMARY KEY,
    entity_id INTEGER,
    source_domain TEXT NOT NULL,
    target_domain TEXT NOT NULL,
    operation TEXT,
    signal_type TEXT NOT NULL DEFAULT 'request'
        CHECK (signal_type IN ('request', 'response', 'event', 'heartbeat', 'alert', 'block')),
    result TEXT NOT NULL DEFAULT 'allowed'
        CHECK (result IN ('allowed', 'denied', 'rate_limited', 'modulated', 'error')),
    conductance_applied FLOAT,
    nt_state JSONB,
    metadata JSONB DEFAULT '{}'::jsonb,
    latency_ms FLOAT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_domain_signal_log_entity
    ON domain_signal_log(entity_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_domain_signal_log_result
    ON domain_signal_log(result) WHERE result != 'allowed';
CREATE INDEX IF NOT EXISTS idx_domain_signal_log_domains
    ON domain_signal_log(source_domain, target_domain, created_at DESC);

CREATE TABLE IF NOT EXISTS trust_ledger (
    id SERIAL PRIMARY KEY,
    entity_id INTEGER NOT NULL UNIQUE,
    trust_score FLOAT NOT NULL DEFAULT 0.5
        CHECK (trust_score >= 0 AND trust_score <= 1.0),
    total_requests BIGINT DEFAULT 0,
    successful_requests BIGINT DEFAULT 0,
    denied_requests BIGINT DEFAULT 0,
    violations INTEGER DEFAULT 0,
    last_activity_at TIMESTAMPTZ DEFAULT NOW(),
    last_decay_at TIMESTAMPTZ DEFAULT NOW(),
    trust_tier TEXT GENERATED ALWAYS AS (
        CASE
            WHEN trust_score < 0.2 THEN 'quarantined'
            WHEN trust_score < 0.5 THEN 'provisional'
            WHEN trust_score < 0.7 THEN 'standard'
            WHEN trust_score < 0.9 THEN 'trusted'
            ELSE 'voucher'
        END
    ) STORED,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS entity_domain_access (
    id SERIAL PRIMARY KEY,
    entity_id INTEGER NOT NULL,
    domain_id TEXT NOT NULL REFERENCES domain_registry(id),
    access_level TEXT NOT NULL DEFAULT 'none'
        CHECK (access_level IN ('none', 'read', 'read_write', 'execute', 'admin')),
    scope_constraints JSONB DEFAULT '{}'::jsonb,
    trust_floor FLOAT DEFAULT 0.0,
    granted_by TEXT,
    grant_reason TEXT,
    expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(entity_id, domain_id)
);

-- ── Memory network protocol tables ────────────────────────────────────

CREATE TABLE IF NOT EXISTS platform_audit_chain (
    op_id          UUID PRIMARY KEY,
    actor          TEXT NOT NULL,
    op             TEXT NOT NULL,
    payload        JSONB NOT NULL,
    prev_hash      BYTEA,
    hmac           BYTEA NOT NULL,
    ts             TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_platform_audit_chain_ts
    ON platform_audit_chain (ts DESC);
CREATE INDEX IF NOT EXISTS idx_platform_audit_chain_actor_op
    ON platform_audit_chain (actor, op, ts DESC);

CREATE TABLE IF NOT EXISTS memory_subscriptions (
    entity_id           INTEGER PRIMARY KEY,
    subscriber_endpoint TEXT,
    sequence_cursor     BIGINT NOT NULL DEFAULT 0,
    last_ack_at         TIMESTAMPTZ DEFAULT NOW(),
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS config_versions (
    entity_id          INTEGER NOT NULL,
    field_path         TEXT NOT NULL,
    sequence           BIGINT NOT NULL DEFAULT 1,
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_by         TEXT,
    PRIMARY KEY (entity_id, field_path)
);

-- ── Seed domain registry (14 domains) ─────────────────────────────────
INSERT INTO domain_registry (id, name, brain_analog, domain_group, description) VALUES
    ('core.entity',       'Entity',           'self',               'core',   'The entity itself — source of all internal thought and action'),
    ('core.cognitive',    'Cognitive',        'prefrontal_cortex',  'core',   'Inference / reasoning surface'),
    ('core.memory',       'Memory',           'hippocampus',        'core',   'Long-term + working memory'),
    ('core.governance',   'Governance',       'amygdala',           'core',   'Constitutional rules, kill switch, audit'),
    ('bots.execution',    'Bot Execution',    'motor_cortex',       'bots',   'Skill / bot dispatch surface'),
    ('infra.codebase',    'Codebase',         'parietal_cortex',    'infra',  'Source-code reads / edits / builds'),
    ('infra.oracle',      'Oracle',           'occipital_cortex',   'infra',  'Memory pipeline manager'),
    ('infra.database',    'Database',         'thalamus',           'infra',  'Direct DB access'),
    ('infra.network',     'Network',          'cerebellum',         'infra',  'Network ops / DNS / routing'),
    ('container.relay',   'Container Relay',  'brain_stem',         'core',   'Container-to-bridge tunnel'),
    ('device.filesystem', 'Filesystem',       'motor_cortex',       'device', 'Local filesystem ops'),
    ('external.mcp',      'External MCP',     'sensory_cortex',     'io',     'External MCP clients (Claude Desktop, Cursor)'),
    ('external.operator', 'External Operator','sensory_cortex',     'io',     'Operator''s cseries-gateway endpoint'),
    ('synaptive.shape',   'Synaptive Shape',  'prefrontal_cortex',  'core',   'Synaptive identity surface for external chats')
ON CONFLICT (id) DO NOTHING;

-- ── Seed synapses (incl. the 3 new memory-protocol synapses) ──────────
INSERT INTO domain_synapse (source_domain, target_domain, synapse_type, base_conductance, description) VALUES
    ('core.entity',     'core.cognitive',     'direct', 1.0, 'Entity invokes its own inference'),
    ('core.entity',     'core.memory',        'direct', 1.0, 'Memory is identity — unconditional'),
    ('core.entity',     'core.governance',    'direct', 1.0, 'Self-policy reads + behavioral broadcasts'),
    ('core.entity',     'bots.execution',     'direct', 1.0, 'Skill / bot dispatch'),
    ('core.entity',     'infra.codebase',     'direct', 1.0, 'Read / edit / build the codebase'),
    ('core.entity',     'infra.oracle',       'direct', 1.0, 'Memory-pipeline operations'),
    ('core.entity',     'infra.database',     'direct', 1.0, 'Direct DB ops'),
    ('core.entity',     'infra.network',      'direct', 1.0, 'Network operations'),
    ('core.entity',     'external.operator',  'gated',  1.0, 'memory.entity.distill — projection to operator'),
    ('core.governance', 'core.entity',        'gated',  1.0, 'PME-initiated config.push to subscribed EME'),
    ('container.relay', 'device.filesystem',  'direct', 1.0, 'Relay-driven filesystem ops'),
    ('container.relay', 'core.governance',    'gated',  1.0, 'Relay-initiated governance reads'),
    ('container.relay', 'container.relay',    'direct', 1.0, 'Relay self-loop'),
    ('core.governance', 'core.governance',    'direct', 1.0, 'Governance self-loop (kill-switch path)'),
    ('external.mcp',    'synaptive.shape',    'gated',  1.0, 'External shape() invocation')
ON CONFLICT (source_domain, target_domain) DO NOTHING;

-- ── Per-entity self-access grants (chat depends on these) ─────────────
-- infra.codebase is NOT a self grant (2026-10-04): it put the platform's
-- source, image, service and bridge-DaemonSet tools in every Host's tool
-- list. The AUTHORITY is the central entity_domain_access row, which the
-- bridge enforces per op (the bridge adapter's
-- _refuse_platform_op). The LIST still reads this pod table
-- (runtime/tools.py, the infra.codebase=admin probe), so a Host that should
-- see those tools needs a row here as well. No seed writes it: the kernel
-- copies the central level into it (server/platform_grant_sync.py).
INSERT INTO entity_domain_access (entity_id, domain_id, access_level, granted_by, grant_reason)
SELECT
    er.id,
    d.domain_id,
    d.access_level,
    'migration:v019',
    'self — every entity needs to invoke its own cognition + memory'
FROM (SELECT id FROM entity_registry LIMIT 1) er
CROSS JOIN (VALUES
    ('core.entity',      'admin'),
    ('core.cognitive',   'execute'),
    ('core.memory',      'admin'),
    ('core.governance',  'read'),
    ('bots.execution',   'execute'),
    ('container.relay',  'read_write'),
    ('external.operator','read_write')
) AS d(domain_id, access_level)
ON CONFLICT (entity_id, domain_id) DO UPDATE
  SET access_level = EXCLUDED.access_level
  WHERE entity_domain_access.access_level = 'none';
