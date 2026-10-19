-- v047 (2026-05-31): repair Connections > Network Services (GET /api/network/state 500).
--
-- Root cause: server/network/state.py defines the network_* tables in
-- ensure_tables(), but ensure_tables() is NEVER CALLED anywhere — it's dead.
-- The tables only exist on entities where a long-ago version did call it, and
-- those tables predate the `name` column that was later added to the DDL.
-- list_custom_services() SELECTs `name` -> asyncpg UndefinedColumnError
-- 'column "name" does not exist' -> the whole Network Services tab 500s.
-- Fresh entities can lack the tables entirely (relation-does-not-exist 500).
--
-- This migration is the canonical, idempotent fix on the entrypoint.sh path
-- (runs once per entity, recorded in schema_migrations): create both tables
-- if missing (fresh entities) AND backfill the columns onto pre-existing
-- tables (old entities). CREATE TABLE IF NOT EXISTS does not alter an
-- existing table, which is exactly why the column drift was never repaired.

CREATE TABLE IF NOT EXISTS network_service_state (
    entity_id   INTEGER NOT NULL,
    service_id  TEXT NOT NULL,
    enabled     BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (entity_id, service_id)
);

CREATE TABLE IF NOT EXISTS network_custom_services (
    id           SERIAL PRIMARY KEY,
    entity_id    INTEGER NOT NULL,
    name         TEXT NOT NULL,
    host         TEXT NOT NULL,
    proposal_id  TEXT,
    status       TEXT NOT NULL DEFAULT 'pending',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_at   TIMESTAMPTZ
);

-- Backfill columns onto tables created before these columns existed.
ALTER TABLE network_custom_services ADD COLUMN IF NOT EXISTS name        TEXT;
ALTER TABLE network_custom_services ADD COLUMN IF NOT EXISTS proposal_id TEXT;
ALTER TABLE network_custom_services ADD COLUMN IF NOT EXISTS status      TEXT NOT NULL DEFAULT 'pending';
ALTER TABLE network_custom_services ADD COLUMN IF NOT EXISTS created_at  TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE network_custom_services ADD COLUMN IF NOT EXISTS decided_at  TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS ix_network_custom_entity_status
    ON network_custom_services(entity_id, status);
CREATE UNIQUE INDEX IF NOT EXISTS ux_network_custom_entity_host
    ON network_custom_services(entity_id, host)
    WHERE status IN ('pending', 'approved');
