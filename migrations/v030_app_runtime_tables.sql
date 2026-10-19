-- Migration v030: promote the four app-runtime tables from Python
-- `ensure_schema()` helpers into a real migration file.
--
-- Source of truth was previously embedded `CREATE TABLE IF NOT EXISTS`
-- blocks inside the modules that own each table:
--
--   server/applications/storage.py        -> app_config
--   server/applications/trust_store.py    -> application_signing_keys
--   server/applications/registry.py       -> installed_applications
--   server/applications/update_check.py   -> update_notifications
--
-- This made schema changes invisible to the migration tracker, with no
-- order guarantee, no rollback, and no record in schema_migrations. B4
-- from the 2026-05-22 audit.
--
-- Every CREATE here matches the Python blob byte-for-byte (verified at
-- write time). The Python `ensure_schema()` calls stay in place as
-- idempotent safety nets — they no-op once this migration runs because
-- the tables already exist.
--
-- On entities that have been chatting for weeks: the tables already
-- exist (Python created them long ago). `IF NOT EXISTS` makes this a
-- no-op there; schema_migrations gets the v030 row and skips next boot.
--
-- On fresh entities: the migration runs at entrypoint.sh time, BEFORE
-- the Python app code starts. Tables exist by the time ensure_schema()
-- is called, which then no-ops.

BEGIN;

-- ── app_config (per-app key/value storage) ───────────────────────
CREATE TABLE IF NOT EXISTS app_config (
    app_id      TEXT        NOT NULL,
    key         TEXT        NOT NULL,
    value       JSONB       NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (app_id, key)
);

-- ── application_signing_keys (Ed25519 trust store) ───────────────
CREATE TABLE IF NOT EXISTS application_signing_keys (
    fingerprint     TEXT        PRIMARY KEY,
    public_key      BYTEA       NOT NULL,
    publisher       TEXT        NOT NULL,
    tier_allowed    TEXT[]      NOT NULL,
    registered_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at      TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_application_signing_keys_active
    ON application_signing_keys (revoked_at)
    WHERE revoked_at IS NULL;

-- ── installed_applications (the per-entity app registry) ─────────
CREATE TABLE IF NOT EXISTS installed_applications (
    id                TEXT        PRIMARY KEY,
    name              TEXT        NOT NULL,
    version           TEXT        NOT NULL,
    tier              TEXT        NOT NULL
        CHECK (tier IN ('native','standard','premium','community','custom')),
    entrypoint        TEXT        NOT NULL,
    permissions       JSONB       NOT NULL DEFAULT '[]'::jsonb,
    source_dir        TEXT,
    source_signature  TEXT,
    installed_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    upgraded_at       TIMESTAMPTZ,
    status            TEXT        NOT NULL DEFAULT 'active'
        CHECK (status IN ('active','disabled'))
);

CREATE INDEX IF NOT EXISTS idx_installed_applications_tier
    ON installed_applications (tier);

-- ── update_notifications (once-per-version dedupe for app updates) ──
CREATE TABLE IF NOT EXISTS update_notifications (
    app_id              TEXT NOT NULL,
    installed_version   TEXT NOT NULL,
    available_version   TEXT NOT NULL,
    notified_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (app_id, installed_version, available_version)
);

COMMIT;
