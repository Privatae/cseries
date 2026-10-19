-- v069 (2026-06-23): C1 — REVERSIBLE box-level NAC quarantine.
--
-- A quarantine cuts a bastion's ENTIRE WireGuard mesh reach via the NAC engine,
-- reinstatable WITHOUT destroying the bearer/identity. It is the missing rung
-- between the two existing levers:
--
--   contain_agent      — surgical: SIGTERM one detected pid (topology.request_contain)
--   nac_mgmt suspend   — reversible, but ONLY the mgmt(P8.1)+harness(H4) lanes deny;
--                        the WG tunnel (egress/inbound) stays up
--   QUARANTINE (this)  — reversible WHOLE-MESH cut: drops the concentrator peer so the
--                        tunnel goes dead AND the mgmt/harness gates deny, WITHOUT
--                        revoking the bearer in mcp_client_tokens (that survives, so a
--                        release brings the box back with no re-enrol)
--   revoke_bastion     — DESTRUCTIVE: revokes the bearer; the box must re-enrol
--
-- Posture: quarantine is a TIGHTEN (no step-up — tightening is always safe); release
-- is a WIDEN (step-up TOTP, the mirror of nac_mgmt resume — a manipulated agent can't
-- produce the user's code, so it can't release itself). Both are ALWAYS audited via the
-- nac_agent_audit trail (nac_agents.audit_disposition), disposition='quarantine'/'release'.
--
-- The row is keyed by `server` (hostname) — the same key the gate short-circuits on,
-- the mirror of nac_bastion_suspend. token_id is recorded for the audit/UI join only.
-- Sovereign: the host's own record; the platform never sees it.

CREATE TABLE IF NOT EXISTS nac_bastion_quarantine (
    server         TEXT PRIMARY KEY,
    token_id       INTEGER,
    quarantined_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    quarantined_by TEXT
);
