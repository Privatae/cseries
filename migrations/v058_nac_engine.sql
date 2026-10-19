-- v058: Bastion-mesh NAC (Network Access Control) — host-local engine, P0.
--
-- The host's OWN network access-control policy for its bastion mesh (the user's
-- AI-powered VPN). Pod-local + single-tenant: every row is THIS host's. The platform
-- never stores, reads, decides, or signs it (sovereignty; the authoritative wall is
-- AT the container, so even a compromised bastion/concentrator can't exceed what the
-- host allows). This SUPERSEDES the reverted platform build that put these tables in
-- the privatae DB + the concentrator forwarder + bridge signing (that inversion was
-- rejected).
--
-- NAC enforces NETWORK POLICY; ceigas-local enforces CAPABILITY — two host-local
-- enforcers, different axis. NAC is NOT Circles (peer slips) and NOT the domain_policy
-- table (v048 — peer inbound reachability for the inbox). No platform GRANTs (the pod
-- user `entity` owns the pod database), no entity_id column (pod-local = implicitly this host),
-- per the v048 in-pod convention.
--
-- Model (overlay NAC; logic ported from the reverted build, relocated host-local):
--   nac_profiles  = the "what access" bundles the PDP assigns (allow grants `permits`;
--                   quarantine denies all).
--   nac_policy    = ordered rules (conditions -> profile), FIRST-MATCH-WINS by priority.
--                   Authority: host PROPOSES (inert) -> USER APPROVES
--                   (user-gate; allow/widen needs step-up, P4) -> host signs the approved
--                   terms with its OWN entity key for tamper-evidence (P4). The PDP loads
--                   only approved + non-revoked rows; a signed row whose stored terms no
--                   longer match its signature makes the mesh FAIL CLOSED.
--   nac_endpoints = per-device (bastion) posture + the PDP's last decision (CoA state),
--                   keyed on the bastion's mcp_client_tokens.id (the in-pod device id).
--
-- Default-preserve: a host with NO governing policy PERMITS (today's behavior) — the
-- engine is behavior-neutral until the user authors policy. Strict default-deny is an
-- explicit low-priority empty-match 'quarantine' catch-all the user adds, not a mode.

-- ── Authorization profiles — the "what access" bundles the PDP assigns ────────
CREATE TABLE IF NOT EXISTS nac_profiles (
    id            SERIAL PRIMARY KEY,
    name          TEXT NOT NULL,
    description   TEXT,
    enforcement   TEXT NOT NULL DEFAULT 'allow'        -- 'allow' grants `permits`; 'quarantine' denies all
                      CHECK (enforcement IN ('allow','quarantine')),
    is_default    BOOLEAN NOT NULL DEFAULT FALSE,      -- assigned when policy exists but no rule matches
    -- permits: JSONB list of reachability grants this profile confers, e.g.
    --   [{"direction":"inbound","target":"self-host","lane":"mcp","ports":"8443"}]
    permits       JSONB NOT NULL DEFAULT '[]',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at    TIMESTAMPTZ,
    UNIQUE (name)
);

-- ── Policy — the PDP ruleset: conditions -> profile (first match wins) ────────
CREATE TABLE IF NOT EXISTS nac_policy (
    id            SERIAL PRIMARY KEY,
    name          TEXT,
    priority      INT NOT NULL DEFAULT 100,            -- lower evaluated first
    -- match: all-of conditions (JSONB). Recognized optional keys:
    --   "fingerprint":    exact device hardware fingerprint (the MCP-lane identity)
    --   "classification": [list]   (endpoint profiled type: server/workstation)
    --   "posture":        [healthy|degraded|unknown]
    --   "min_posture":    "healthy"|"degraded"   (posture rank >= this)
    -- empty match {} = catch-all; the user's explicit low-priority {} -> quarantine
    -- profile is the strict default-deny.
    match         JSONB NOT NULL DEFAULT '{}',
    profile_id    INT NOT NULL REFERENCES nac_profiles(id),
    -- Authority keystone: host proposes -> user approves (step-up
    -- for allow, P4). approved_by = the user subject (off-host Authentik) that approved.
    proposed_by   TEXT NOT NULL DEFAULT 'host'
                      CHECK (proposed_by IN ('host','user')),
    approved_at   TIMESTAMPTZ,
    approved_by   TEXT,
    stepup_at     TIMESTAMPTZ,                          -- when an allow received step-up (P4)
    -- Tamper-evidence (host-local, NOT bridge): Ed25519 over canonical(signed_terms)
    -- with the host's OWN entity signing key. signed_terms carries the full effective
    -- rule {name,priority,match,enforcement,permits} and is AUTHORITATIVE once signed.
    -- Armed in P4; until then rows are approved-but-unsigned and load (unarmed bootstrap).
    signing_key_id TEXT,
    signed_terms  JSONB,
    signature     TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_nac_policy_live
    ON nac_policy (priority)
    WHERE approved_at IS NOT NULL AND revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_nac_policy_draft
    ON nac_policy (id)
    WHERE approved_at IS NULL AND revoked_at IS NULL;

-- ── Endpoint state — per-device posture + the PDP's continuous decision (CoA) ─
-- Keyed on the bastion's mcp_client_tokens.id (operator_kind='bastion'). Kept
-- separate from the token registry so the engine's mutable state doesn't contend
-- with enrollment. Posture is written from the heartbeat (P2); assigned_profile +
-- decided_at are written by the PDP (P3/P5).
CREATE TABLE IF NOT EXISTS nac_endpoints (
    token_id            INT PRIMARY KEY,                -- bastion's mcp_client_tokens.id
    fingerprint         TEXT,                           -- device hardware fingerprint
    classification      TEXT,                           -- profiled type (hostname/platform)
    hostname            TEXT,
    platform            TEXT,
    posture             TEXT NOT NULL DEFAULT 'unknown'
                            CHECK (posture IN ('healthy','degraded','unknown')),
    posture_at          TIMESTAMPTZ,
    assigned_profile_id INT REFERENCES nac_profiles(id),
    assigned_profile    TEXT,                           -- denormalized name (UI/log)
    decided_at          TIMESTAMPTZ,
    last_decision       TEXT,                           -- short why-string for the UI
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_nac_endpoints_fingerprint ON nac_endpoints (fingerprint);
