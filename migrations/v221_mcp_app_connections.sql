-- v221: an MCP server an installed app runs for its OWN Host
-- (server/applications/manifest.py McpSpec,
-- server/mcp_connections.py sync_app).
--
-- A process app that declares `mcp: {path: /mcp}` serves MCP from its own
-- runtime pod, in the person's namespace. Installing it makes it one of the
-- Host's MCP connections, named after the app, the way a remote server becomes
-- one when the person approves its egress rule. The differences are all in
-- where the traffic goes:
--
--   app_id   the installed app that serves it. NULL for a remote server.
--   url      app://<app_id><path> for an app connection: the kernel reaches
--            the app's pod over the pod network (the address comes from the
--            app's wake, harness_backend.ensure_awake_app), never through the
--            egress gate, so no egress rule is drafted and rule_id is NULL.
--            A remote server's URL stays https only.
--
-- The rest of the row is the same: signed terms (they carry the app id), the
-- tool list and its hash, and the Host's skills/mcp-<name> gate on calls.

BEGIN;

ALTER TABLE mcp_connections ADD COLUMN IF NOT EXISTS app_id TEXT;

-- v212 wrote the URL rule as a column CHECK, which Postgres names
-- mcp_connections_url_check. Replaced by one that admits app:// for an app row.
ALTER TABLE mcp_connections DROP CONSTRAINT IF EXISTS mcp_connections_url_check;
ALTER TABLE mcp_connections ADD CONSTRAINT mcp_connections_url_check CHECK (
    (app_id IS NULL AND url LIKE 'https://%')
    OR (app_id IS NOT NULL AND url LIKE 'app://' || app_id || '/%'));

-- One live connection per app (its name is the app id, already unique per
-- Host; this keeps a renamed row from doubling it).
CREATE UNIQUE INDEX IF NOT EXISTS uq_mcp_connections_live_app
    ON mcp_connections (entity_id, app_id)
    WHERE app_id IS NOT NULL AND status <> 'revoked';

COMMIT;
