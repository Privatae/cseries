-- v065 (2026-06-20): NAC agent governance P9.3 — containment.
--
-- observe (P9.0/9.1) -> reconcile (P9.2) -> CONTAIN. The host queues a containment for a detected
-- agent (token_id, pid); the bastion's next heartbeat carries {"contain":[pids]} and the bastion
-- SIGTERMs each pid that is currently a detected agent, refusing any pid that isn't (the host can
-- only contain agents it observed, never an arbitrary process). Host-directed (the directive rides
-- the authenticated mTLS heartbeat) — the user containing activity on the user's own server. This
-- is the surgical containment; revoking the bastion is the broad "cut the whole server's mesh
-- reach" kill. The runtime ensures this table defensively (topology.ensure_table); authoritative
-- record here.
CREATE TABLE IF NOT EXISTS nac_agent_contain (
    id           BIGSERIAL PRIMARY KEY,
    token_id     INTEGER NOT NULL,
    pid          INTEGER NOT NULL,
    requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    cleared_at   TIMESTAMPTZ
);
