-- v255 (2026-10-28): outside agents connected to this Host over MCP.
--
-- Any outside agent (Claude Code, Codex, an agent inside T3 Code) reaches the
-- person's machines only by asking the Host over MCP. What the person approved
-- for that agent runs; anything new comes back as a link to a page on the Host
-- where the person approves it.
--
-- The Host is the agent's OAuth authorization server (server/agent_access):
--   agent_connections  one per registered client (RFC 7591), connected once the
--                      person approves the connect page; revoked on its own
--   agent_grants       what one connection may do without asking: 'read' (the
--                      machine list and read-only ops) or 'op' (one op on one
--                      machine, given with "Always for this agent")
--   agent_codes        one-time authorization codes, bound to a PKCE challenge
--   agent_tokens       access and refresh tokens
--   agent_asks         a request the agent made outside its grants, or one a
--                      machine is holding for the person's approval
--   agent_calls        every call, for the person to read
--
-- Codes and tokens are stored as the SHA-256 of a 256-bit random value, never
-- the value (as mcp_client_tokens is). The authorization rows are carried over
-- a restore (server/host_restore.CARRIED_TABLES): a backup never brings back a
-- connection the person revoked after it was taken.
--
-- Not mirrored in init-db.sql: like v254, the migration is the only place.

BEGIN;

CREATE TABLE IF NOT EXISTS agent_connections (
    id             BIGSERIAL PRIMARY KEY,
    entity_id      INTEGER NOT NULL,
    client_id      TEXT NOT NULL UNIQUE,
    client_name    TEXT NOT NULL DEFAULT '',
    redirect_uris  JSONB NOT NULL DEFAULT '[]'::jsonb,
    registered_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    connected_at   TIMESTAMPTZ,
    revoked_at     TIMESTAMPTZ,
    last_used_at   TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_agent_connections_live
    ON agent_connections (entity_id) WHERE revoked_at IS NULL;

CREATE TABLE IF NOT EXISTS agent_grants (
    id             BIGSERIAL PRIMARY KEY,
    connection_id  BIGINT NOT NULL REFERENCES agent_connections(id) ON DELETE CASCADE,
    kind           TEXT NOT NULL CHECK (kind IN ('read', 'op')),
    server         TEXT NOT NULL DEFAULT '*',
    op             TEXT NOT NULL DEFAULT '*',
    granted_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at     TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_grants_live
    ON agent_grants (connection_id, kind, server, op) WHERE revoked_at IS NULL;

CREATE TABLE IF NOT EXISTS agent_codes (
    code_hash       BYTEA PRIMARY KEY,
    connection_id   BIGINT NOT NULL REFERENCES agent_connections(id) ON DELETE CASCADE,
    redirect_uri    TEXT NOT NULL,
    code_challenge  TEXT NOT NULL,
    resource        TEXT NOT NULL DEFAULT '',
    expires_at      TIMESTAMPTZ NOT NULL,
    used_at         TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS agent_tokens (
    token_hash     BYTEA PRIMARY KEY,
    connection_id  BIGINT NOT NULL REFERENCES agent_connections(id) ON DELETE CASCADE,
    kind           TEXT NOT NULL CHECK (kind IN ('access', 'refresh')),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at     TIMESTAMPTZ NOT NULL,
    revoked_at     TIMESTAMPTZ
);
-- The authorization code a token descends from (its code_hash), carried
-- through every refresh: a code presented a second time revokes them all
-- (RFC 6749 §4.1.2). No foreign key: a code row may be pruned before its
-- tokens expire. Added in place for a database that took v255 before it.
ALTER TABLE agent_tokens ADD COLUMN IF NOT EXISTS from_code BYTEA;
CREATE INDEX IF NOT EXISTS ix_agent_tokens_connection ON agent_tokens (connection_id);
CREATE INDEX IF NOT EXISTS ix_agent_tokens_from_code ON agent_tokens (from_code)
    WHERE from_code IS NOT NULL;

CREATE TABLE IF NOT EXISTS agent_asks (
    id             TEXT PRIMARY KEY,
    connection_id  BIGINT NOT NULL REFERENCES agent_connections(id) ON DELETE CASCADE,
    kind           TEXT NOT NULL CHECK (kind IN ('grant', 'machine')),
    server         TEXT NOT NULL,
    op             TEXT NOT NULL,
    args           JSONB NOT NULL DEFAULT '[]'::jsonb,
    args_digest    TEXT NOT NULL,
    note           TEXT NOT NULL DEFAULT '',
    -- kind 'machine': what the machine's own approval is waiting on
    -- (nac_approval.pending_result: a ticket id, or the device/console words)
    waiting        JSONB NOT NULL DEFAULT '{}'::jsonb,
    state          TEXT NOT NULL DEFAULT 'waiting'
                   CHECK (state IN ('waiting', 'once', 'always', 'refused', 'spent')),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at     TIMESTAMPTZ NOT NULL,
    decided_at     TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_agent_asks_open
    ON agent_asks (connection_id, server, op, args_digest) WHERE state IN ('waiting', 'once');

CREATE TABLE IF NOT EXISTS agent_calls (
    id             BIGSERIAL PRIMARY KEY,
    connection_id  BIGINT NOT NULL REFERENCES agent_connections(id) ON DELETE CASCADE,
    tool           TEXT NOT NULL,
    server         TEXT NOT NULL DEFAULT '',
    op             TEXT NOT NULL DEFAULT '',
    outcome        TEXT NOT NULL,
    at             TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_agent_calls_connection ON agent_calls (connection_id, at DESC);

COMMIT;
