-- v061 (2026-06-20): NAC agent governance (P9) — the bastion as agent OBSERVER.
--
-- The management lane's future is agent GOVERNANCE, not a remote-command op-catalog: the user
-- runs whatever mature agent they want (Claude Code, Cursor, …) on a box they own, and the bastion
-- OBSERVES + CONTAINS it. P9.0 = the OBSERVE plane: the bastion (root, on the server) scans /proc
-- for known agent signatures and reports them on its heartbeat; the host records them here.
--
-- Detection is SOFT + evadable — for visibility + early-warning, NEVER relied on for security.
-- Containment is the host-local NAC (the hard wall). The runtime ensures this table defensively
-- (server/network/nac_agents.py ensure_table) so a pod mid-migration never 500s; this is the
-- authoritative record. The visibility serves the USER's own host on the user's own infra — the
-- platform never sees it (sovereignty).
CREATE TABLE IF NOT EXISTS nac_agent_activity (
    id               BIGSERIAL PRIMARY KEY,
    token_id         INTEGER NOT NULL,              -- the reporting bastion (mcp_client_tokens.id)
    hostname         TEXT,
    pid              INTEGER NOT NULL,
    agent_user       TEXT,
    signature        TEXT NOT NULL,                 -- e.g. claude-code, cursor, aider
    cmdline          TEXT,
    delegation_token TEXT,                          -- P9.2: requested-vs-actual reconciliation tag
    first_seen       TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (token_id, pid, signature)
);
