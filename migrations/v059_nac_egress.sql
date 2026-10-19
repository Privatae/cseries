-- v059 (2026-06-20): NAC egress — the USER's outbound layer (P6).
--
-- The same NAC idea as the inbound wall, pointed the other way: the host's outbound
-- internet flows through ONE routing point (the in-pod egress proxy), and a SIGNED
-- policy decides. The USER authors these rules through the user-gate (host proposes ->
-- user approves; WIDENING reach needs the TOTP step-up, tightening is free) and the host
-- signs them for tamper-evidence -- so whoever signed the rule is whose authority it is.
--
-- Default is WIDE OPEN: with no approved rule, the host reaches the whole internet. The
-- user opts in to limit it. The proxy NEVER logs destinations (sovereignty). The separate
-- "host can't reach the platform's internals" rule is the platform's, enforced at the
-- cluster -- not here.

CREATE TABLE IF NOT EXISTS nac_egress (
    id            SERIAL PRIMARY KEY,
    pattern       TEXT NOT NULL,                      -- destination host: exact "api.x.ai" or suffix "*.x.ai"
    action        TEXT NOT NULL DEFAULT 'allow'
                      CHECK (action IN ('allow','deny')),
    priority      INT  NOT NULL DEFAULT 100,          -- lower evaluated first; first match wins
    note          TEXT,
    -- Authority: host proposes -> user approves (step-up to WIDEN) -> host signs the
    -- approved terms with its entity key for tamper-evidence.
    proposed_by   TEXT NOT NULL DEFAULT 'host'
                      CHECK (proposed_by IN ('host','user')),
    approved_at   TIMESTAMPTZ,
    approved_by   TEXT,
    stepup_at     TIMESTAMPTZ,
    signing_key_id TEXT,
    signed_terms  JSONB,                              -- {pattern, action, priority} the signature covers
    signature     TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_nac_egress_live
    ON nac_egress (priority)
    WHERE approved_at IS NOT NULL AND revoked_at IS NULL;
