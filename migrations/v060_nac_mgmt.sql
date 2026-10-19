-- v060 (2026-06-20): NAC management lane (P8) — the entity OPERATES the servers in its mesh.
--
-- TACACS+ for the entity: per-command authorization + command accounting, decided LOCALLY at
-- each box (the bastion), the platform never in the path. The transport is the egress
-- reverse-relay flipped to a command job type (/bastion-mgmt). Posture is the INVERSE of
-- egress: egress is default-OPEN (the user's sovereignty to reach out, never logged);
-- management is default-DENY and ALWAYS audited (the entity acting ON the user's machine).
--
-- The runtime ensures both tables defensively (server/network/nac_mgmt.py ensure_grants_table,
-- server/routes/network.py _MGMT_AUDIT_DDL) so a pod mid-migration never 500s; this migration
-- is the authoritative record.

-- P8.1: signed grants. The host proposes a draft; the USER approves it (ALWAYS step-up — granting
-- an entity rights ON a server is a WIDEN); the host signs {entity, server, ops, expires} with its
-- entity key. The gate verifies the signature and enforces the SIGNED terms (a forged/expired
-- grant is skipped, which DENIES — the safe direction, the mirror of egress's default-open).
CREATE TABLE IF NOT EXISTS nac_mgmt_grants (
    id             BIGSERIAL PRIMARY KEY,
    entity_id      INTEGER,
    server         TEXT NOT NULL,                 -- a hostname or '*' (every server in the mesh)
    ops            JSONB NOT NULL DEFAULT '[]',    -- the op ids this grant authorizes
    note           TEXT,
    proposed_by    TEXT NOT NULL DEFAULT 'host',
    approved_at    TIMESTAMPTZ,
    approved_by    TEXT,
    stepup_at      TIMESTAMPTZ,
    signing_key_id TEXT,
    signed_terms   JSONB,
    signature      TEXT,
    expires_at     TIMESTAMPTZ,
    revoked_at     TIMESTAMPTZ,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- P8.0: command accounting. Every management action ON the user's machine is recorded —
-- who (entity), what (op), where (server), when, result (decision + exit). This is the bright
-- line vs egress, which records NOTHING about where the host went (sovereignty).
CREATE TABLE IF NOT EXISTS nac_mgmt_audit (
    id          BIGSERIAL PRIMARY KEY,
    entity_id   INTEGER,
    server      TEXT,
    op          TEXT,
    decision    TEXT,
    exit_code   INTEGER,
    at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
