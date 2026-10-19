-- v239 (2026-10-18): signing in to an MCP server with OAuth, the MCP
-- authorization spec (revision 2026-07-28, basic/authorization), for a
-- connection in mcp_connections (v212) that holds no key of its own.
--
-- A server that wants a sign-in answers 401 with a WWW-Authenticate challenge.
-- The kernel then finds the server's authorization server (Protected Resource
-- Metadata, RFC 9728; the server's metadata, RFC 8414 or OpenID Connect
-- Discovery) and keeps what it found in `mcp_oauth`, one row per connection.
-- The person signs in from the server's card in the Network app; the browser
-- comes back to /api/mcp/oauth/callback with the code (server/mcp_oauth.py).
--
-- The tokens are never in these tables. The access token is a vault entry,
-- `mcp-oauth:<name>:<connection id>`, injected by the vault broker like any
-- key; the refresh token and a client secret (if the server issued one) are
-- `mcp-oauth-refresh:<name>:<connection id>`. Both names are kept for the MCP
-- client: keys_manage refuses to add, revoke or use them.
--
--   status         sign_in_needed: the server asked and nobody has signed in
--                  (or the sign-in ran out: a refused refresh, a step-up).
--                  active: the vault holds a token for this server.
--   resource       the `resource` parameter sent with every authorization and
--                  token request (RFC 8707): the server's canonical URI.
--   issuer         the authorization server's issuer, as its validated
--                  metadata names it. A sign-in is only for this issuer: a
--                  server that names another one needs a new sign-in.
--   client_id, client_registration, redirect_uri
--                  how this Host is known to that authorization server:
--                  'pre' (the person entered a client they registered), 'cimd'
--                  (a Client ID Metadata Document URL) or 'dcr' (Dynamic
--                  Client Registration), and the redirect URI it was
--                  registered with. Bound to `issuer`.
--   scope          the scopes asked for at the last sign-in; challenged_scope
--                  what a 401 or 403 asked for since (a step-up asks for the
--                  union of the two).
--
-- mcp_oauth_pending holds one sign-in between the redirect to the
-- authorization server and the callback: the sha-256 of the `state`, the PKCE
-- verifier and the issuer recorded before the redirect (RFC 9207). Single use,
-- ten minutes.

BEGIN;

CREATE TABLE IF NOT EXISTS mcp_oauth (
    connection_id          BIGINT PRIMARY KEY
                           REFERENCES mcp_connections(id) ON DELETE CASCADE,
    entity_id              INTEGER NOT NULL,
    status                 TEXT NOT NULL DEFAULT 'sign_in_needed'
                           CHECK (status IN ('sign_in_needed', 'active')),
    resource               TEXT NOT NULL CHECK (resource LIKE 'https://%'),
    resource_metadata_url  TEXT,
    issuer                 TEXT CHECK (issuer IS NULL OR issuer LIKE 'https://%'),
    authorization_endpoint TEXT,
    token_endpoint         TEXT,
    registration_endpoint  TEXT,
    iss_parameter_supported BOOLEAN NOT NULL DEFAULT FALSE,
    cimd_supported         BOOLEAN NOT NULL DEFAULT FALSE,
    as_scopes_supported    JSONB,
    resource_scopes        JSONB,
    token_auth_methods     JSONB,
    scope                  TEXT,
    challenged_scope       TEXT,
    client_id              TEXT,
    client_registration    TEXT CHECK (client_registration IN ('pre', 'cimd', 'dcr')),
    client_auth_method     TEXT,
    redirect_uri           TEXT,
    expires_at             TIMESTAMPTZ,
    has_refresh            BOOLEAN NOT NULL DEFAULT FALSE,
    signed_in_at           TIMESTAMPTZ,
    refreshed_at           TIMESTAMPTZ,
    step_ups               INTEGER NOT NULL DEFAULT 0,
    last_error             TEXT,
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- A sign-in is for one authorization server: an active row names it.
    CONSTRAINT mcp_oauth_active_has_issuer
        CHECK (status <> 'active' OR (issuer IS NOT NULL AND client_id IS NOT NULL))
);

CREATE TABLE IF NOT EXISTS mcp_oauth_pending (
    state_hash     TEXT PRIMARY KEY,
    connection_id  BIGINT NOT NULL REFERENCES mcp_connections(id) ON DELETE CASCADE,
    entity_id      INTEGER NOT NULL,
    issuer         TEXT NOT NULL,
    iss_parameter_supported BOOLEAN NOT NULL DEFAULT FALSE,
    code_verifier  TEXT NOT NULL,
    redirect_uri   TEXT NOT NULL,
    client_id      TEXT NOT NULL,
    scope          TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at     TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_mcp_oauth_pending_expiry ON mcp_oauth_pending (expires_at);

COMMIT;

-- The AI's read lane (host_recall, v139) is handed SELECT on every new table
-- by default privileges, and v233, which takes it back from the SECRET tables,
-- runs before this file. mcp_oauth_pending holds PKCE verifiers, so it is
-- taken back here too, where it is created.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'host_recall') THEN
        REVOKE ALL ON public.mcp_oauth_pending FROM host_recall CASCADE;
    END IF;
END $$;
