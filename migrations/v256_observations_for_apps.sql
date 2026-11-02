-- v256 — keep what the bastion already sends about each agent, and the tool
-- servers its agent clients declare.
--
-- Per agent the bastion sends `exe` (the file the kernel says the process is
-- running, never derived from the command line), `unit` (the systemd unit
-- that owns it, linux only) and, in bastion 0.54.0 and later, the account
-- behind it: `owner_class` (domain_user, local_user, service, system,
-- unknown), `owner_admin` and `owner_interactive` (yes, no, unknown).
-- nac_agents.ingest dropped all five. They are kept now so an app reading
-- observations (GET /api/network/observations, read:observations) can tell
-- an agent running as an administrator from one running as a plain user, and
-- a service from a terminal session.
--
-- Per bastion the heartbeat carries `tool_servers` (the tool servers agent
-- clients declare in their config files, correlated with the process list)
-- and `tool_server_scan` (how much was read), inside the same `observe` gate
-- as `schedulers`. Stored the way schedulers are: the list is kept when a
-- beat omits the key, and the scan report is NULL on a beat that did not read.
--
-- NULL on rows from older bastions. Additive and idempotent; forward-only.
-- nac_agent_activity stays SECRET to host_world (v233), so these columns are
-- too. network_bastion_heartbeat stays READABLE: the bastion never sends an
-- environment value or a header, and replaces a secret-shaped argument before
-- the command is kept.

BEGIN;

ALTER TABLE nac_agent_activity ADD COLUMN IF NOT EXISTS exe               TEXT;
ALTER TABLE nac_agent_activity ADD COLUMN IF NOT EXISTS unit              TEXT;
ALTER TABLE nac_agent_activity ADD COLUMN IF NOT EXISTS owner_class       TEXT;
ALTER TABLE nac_agent_activity ADD COLUMN IF NOT EXISTS owner_admin       TEXT;
ALTER TABLE nac_agent_activity ADD COLUMN IF NOT EXISTS owner_interactive TEXT;

ALTER TABLE network_bastion_heartbeat ADD COLUMN IF NOT EXISTS tool_servers     JSONB;
ALTER TABLE network_bastion_heartbeat ADD COLUMN IF NOT EXISTS tool_server_scan JSONB;

COMMIT;
