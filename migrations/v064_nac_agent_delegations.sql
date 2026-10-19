-- v064 (2026-06-20): NAC agent governance P9.2 — reconciliation (requested vs. unrequested).
--
-- A delegation tag is host-issued: minting one + launching an agent with PRIVATAE_AGENT_TOKEN=<token>
-- marks that agent as REQUESTED on a server (or '*'). The bastion observes the tag from the agent's
-- env (only that one var) and reports it; the host reconciles — an observed agent carrying a valid
-- (non-revoked, non-expired) tag for its server is requested, otherwise it is UNREQUESTED ("agent
-- activity you didn't request"). The tag is a SOFT key (a thief could copy it) — for separating
-- sanctioned agents from casual ones, never the security wall (that's the NAC). The runtime ensures
-- this table defensively (nac_agents.ensure_table); this is the authoritative record.
CREATE TABLE IF NOT EXISTS nac_agent_delegations (
    id          BIGSERIAL PRIMARY KEY,
    token       TEXT NOT NULL UNIQUE,
    server      TEXT NOT NULL,                 -- a hostname or '*' (any server in the mesh)
    note        TEXT,
    issued_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ,
    revoked_at  TIMESTAMPTZ
);
