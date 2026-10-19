-- v201 (2026-10-03): the integrations store lives in the Host's own Postgres.
--
-- There is no need to keep a second database.
-- server/integrations/core/storage.py kept the user's connected accounts
-- (Gmail, Calendar, Drive, Contacts, YouTube), their sync cursors, the synced
-- records, the Host's own OAuth clients and the per-app grants in a SQLite
-- file, /app/integrations/integrations.db — a second database beside this one,
-- outside the Host's backups and its replica. The same five tables now sit
-- here. Names carry an integration_ prefix: `connections`, `syncs` and
-- `records` are too general for a database that holds everything else.
--
-- The pod holds one entity's data, as the SQLite file did, so the rows carry
-- no entity_id. Credentials stay in the vault; these rows hold only its refs.
--
-- Timestamps stay TEXT: every caller writes and reads ISO-8601 strings
-- (received_at comes straight from the provider), and text keeps each value
-- exactly as written. connection_config is JSONB — it is always a JSON object.
-- records.data / raw_json and integration_grants.methods stay TEXT: their
-- readers already treat a value that does not parse (storage.get_grant reads
-- an unreadable method list as read-only, never as unrestricted).
--
-- v199 and v200 are taken by branches in flight when this was written
-- (ticket projects sharing a repository; code search in Postgres).
--
-- Any rows in the old file are copied here once at the first start after this
-- ships, counted, and the file is removed (storage.retire_sqlite_store,
-- server/main.py). Every live Host's file was empty on 2026-10-03.

CREATE TABLE IF NOT EXISTS integration_connections (
    id                TEXT PRIMARY KEY,
    provider_key      TEXT NOT NULL,
    user_id           TEXT NOT NULL,
    status            TEXT NOT NULL,
    credentials_ref   TEXT,
    connection_config JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    last_sync_at      TEXT,
    last_error        TEXT
);
CREATE INDEX IF NOT EXISTS ix_integration_connections_user
    ON integration_connections (user_id);
CREATE INDEX IF NOT EXISTS ix_integration_connections_provider
    ON integration_connections (provider_key);

CREATE TABLE IF NOT EXISTS integration_syncs (
    connection_id TEXT NOT NULL REFERENCES integration_connections (id) ON DELETE CASCADE,
    resource      TEXT NOT NULL,
    cursor        TEXT,
    mode          TEXT NOT NULL,
    last_run_at   TEXT,
    next_run_at   TEXT,
    last_error    TEXT,
    PRIMARY KEY (connection_id, resource)
);

CREATE TABLE IF NOT EXISTS integration_records (
    id              TEXT PRIMARY KEY,
    connection_id   TEXT NOT NULL REFERENCES integration_connections (id) ON DELETE CASCADE,
    resource        TEXT NOT NULL,
    canonical_model TEXT,
    data            TEXT NOT NULL,
    raw_json        TEXT,
    received_at     TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_integration_records_conn_res
    ON integration_records (connection_id, resource);
CREATE INDEX IF NOT EXISTS ix_integration_records_received
    ON integration_records (received_at);

-- The OAuth CLIENT this Host authorizes with, one per provider family (every
-- Google manifest authorizes against the same `family: google` client). The
-- secret is in the vault; this row holds its ref.
CREATE TABLE IF NOT EXISTS integration_oauth_clients (
    family          TEXT PRIMARY KEY,
    credentials_ref TEXT NOT NULL,
    label           TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

-- Which app may use which connection. NULL methods is every method the
-- provider allows; a JSON array pins the grant to those HTTP methods.
CREATE TABLE IF NOT EXISTS integration_grants (
    connection_id TEXT NOT NULL REFERENCES integration_connections (id) ON DELETE CASCADE,
    app_id        TEXT NOT NULL,
    methods       TEXT,
    granted_at    TEXT NOT NULL,
    PRIMARY KEY (connection_id, app_id)
);
CREATE INDEX IF NOT EXISTS ix_integration_grants_app
    ON integration_grants (app_id);

-- Each old SQLite file whose rows were copied in, written in the same
-- transaction as the copy (storage.retire_sqlite_store). A file still there at
-- a later start, because removing it failed, is removed without being read
-- again: since the copy the user may have revoked a grant or removed a
-- connection that the file still holds.
CREATE TABLE IF NOT EXISTS integration_store_retired (
    path       TEXT PRIMARY KEY,
    retired_at TEXT NOT NULL,
    copied     JSONB NOT NULL DEFAULT '{}'::jsonb
);
