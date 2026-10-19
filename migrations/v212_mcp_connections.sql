-- v212: the MCP servers a Host connects to, one row per connection.
--
-- The Host drafts a connection with mcp_connections(op='connect')
-- (server/mcp_connections.py). Nothing contacts the server then: the row is a
-- DRAFT and an egress ALLOW rule for the server's host is drafted beside it
-- (nac_egress, rule_id). When the person approves that rule in the Network app
-- with their code (server/network/nac_commit.approve_egress), the connection's
-- terms are signed with the Host's key in the same transaction and the row
-- becomes ACTIVE. Disconnecting, or removing the rule, makes it REVOKED.
--
--   name           the server's name on this Host: mcp:<name>/<tool> names its
--                  tools, skills/mcp-<name> is its skill, and `mcp:<name>` is
--                  the key a turn type holds to use it. Lowercase letters,
--                  digits and single hyphens (the CHECK below), like a skill's.
--   url            the server's MCP endpoint, https only.
--   key_name       the vault entry the kernel injects (bearer, or the header
--                  `auth_header` names). NULL = the server takes no token.
--   rule_id        the egress allow rule drafted with it (nac_egress.id).
--   signed_terms, signature, signing_key_id
--                  {name, url, key, auth, rule_id} signed on approval with the
--                  key nac_egress rules are signed with. The client refuses a
--                  connection whose signature does not verify.
--   era            'modern' (2026-07-28, stateless) or 'legacy' (initialize +
--                  Mcp-Session-Id), as the client found it; protocol_version is
--                  the version it speaks.
--   tools, tools_hash, tools_fetched_at, tools_ttl_ms
--                  the server's tool list as the kernel screened it, the hash
--                  of the list as the server sent it, when it was fetched and
--                  the server's own ttlMs. tools_changed_at is when the hash
--                  last changed after a first fetch: the server's skill written
--                  before then is stale. tools_error is the last fetch failure.

BEGIN;

CREATE TABLE IF NOT EXISTS mcp_connections (
    id                BIGSERIAL PRIMARY KEY,
    entity_id         INTEGER NOT NULL
                      REFERENCES entity_registry(id) ON DELETE CASCADE,
    name              TEXT NOT NULL
                      CHECK (name ~ '^[a-z0-9]+(-[a-z0-9]+)*$' AND length(name) <= 40),
    url               TEXT NOT NULL CHECK (url LIKE 'https://%'),
    key_name          TEXT,
    auth_header       TEXT,
    rule_id           INTEGER,
    status            TEXT NOT NULL DEFAULT 'draft'
                      CHECK (status IN ('draft', 'active', 'revoked')),
    proposed_by       TEXT NOT NULL DEFAULT 'host',
    signing_key_id    TEXT,
    signed_terms      JSONB,
    signature         TEXT,
    approved_at       TIMESTAMPTZ,
    approved_by       TEXT,
    era               TEXT CHECK (era IN ('modern', 'legacy')),
    protocol_version  TEXT,
    tools             JSONB,
    tools_hash        TEXT,
    tools_fetched_at  TIMESTAMPTZ,
    tools_ttl_ms      BIGINT,
    tools_changed_at  TIMESTAMPTZ,
    tools_error       TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at        TIMESTAMPTZ,
    -- An active connection is a signed one.
    CONSTRAINT mcp_connections_active_is_signed
        CHECK (status <> 'active' OR (signed_terms IS NOT NULL AND signature IS NOT NULL))
);

-- One live (draft or active) connection per name.
CREATE UNIQUE INDEX IF NOT EXISTS uq_mcp_connections_live_name
    ON mcp_connections (entity_id, name)
    WHERE status <> 'revoked';

-- approve_egress and revoke_egress look a connection up by its rule.
CREATE INDEX IF NOT EXISTS ix_mcp_connections_rule
    ON mcp_connections (rule_id)
    WHERE status <> 'revoked';

COMMIT;
