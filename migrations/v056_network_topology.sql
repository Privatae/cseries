-- v056 (2026-06-17): Network app data plane — gateway heartbeat + network_audit backstop.
--
-- The Network system app (system-apps/network/) renders the host's sovereign
-- network: its gateways (machines it reaches), peers, egress posture, and usage.
--
-- 1) network_gateway_heartbeat — gateways are mcp_client_tokens rows with
--    operator_kind='gateway' (init-db.sql:1161), but those rows carry NO liveness
--    beyond last_used_at (bumped only on shape() calls — not a tunnel signal) and
--    no machine identity. This structured table is what each gateway daemon POSTs
--    to (POST /api/network/gateways/heartbeat, internal-auth). last_seen_at — not
--    last_used_at — is the liveness signal the UI reads. Keyed by token_id so a
--    revoked/rotated gateway token drops its heartbeat with it.
--
-- 2) network_audit — the egress audit table's only creator is audit.ensure_table()
--    (server/network/audit.py:54), which has NO caller, so on a fresh pod GET
--    /api/network/audit can 500 / the DB sink silently no-ops. Create it here on
--    the authoritative migration path (idempotent; matches _AUDIT_TABLE_DDL exactly)
--    so the Egress "activity" view is reliable.

CREATE TABLE IF NOT EXISTS network_gateway_heartbeat (
    token_id        INTEGER PRIMARY KEY,   -- mcp_client_tokens.id (same brain DB)
    hostname        TEXT,
    gateway_version TEXT,
    platform        TEXT,                  -- os/arch the gateway reports
    tunnel_status   TEXT,                  -- up|down|degraded (gateway-reported)
    reported_ip     TEXT,
    wg_pubkey       TEXT,                  -- gateway-reported metadata (no WG data plane today)
    capabilities    JSONB NOT NULL DEFAULT '[]',  -- e.g. ["exec"] — opt-in consent for future actuation
    last_seen_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_gateway_hb_last_seen ON network_gateway_heartbeat(last_seen_at DESC);

-- Egress audit backstop (idempotent — see audit.py _AUDIT_TABLE_DDL).
CREATE TABLE IF NOT EXISTS network_audit (
    id          BIGSERIAL PRIMARY KEY,
    ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
    method      TEXT NOT NULL,
    host        TEXT NOT NULL,
    port        INTEGER NOT NULL,
    allow       BOOLEAN NOT NULL,
    posture     TEXT NOT NULL,
    service_id  TEXT,
    reason      TEXT
);
CREATE INDEX IF NOT EXISTS ix_network_audit_ts   ON network_audit(ts DESC);
CREATE INDEX IF NOT EXISTS ix_network_audit_host ON network_audit(host, ts DESC);
