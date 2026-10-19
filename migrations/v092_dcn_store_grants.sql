-- v092 — DCN STORE GRANTS (CN-1): dcn_stores becomes a NAC-governed grant object.
--        (the DCN design: cabinet composite, grant shape, NAC integration, CN-1.)
--
-- v091 shipped the route-map tables inert. This adds the grant lifecycle columns —
-- the SAME shape as nac_mgmt_grants (v060): propose (draft) → user approves with the
-- sovereign TOTP step-up (ALWAYS — creating or widening a store is a WIDEN, §7) →
-- terms signed with the host's entity key → the pod gate verifies the signature and
-- enforces the SIGNED terms, default-deny (load_dcn_grants mirrors load_mgmt_grants'
-- fail-closed posture). Narrow/revoke = TIGHTEN, free.
--
-- Cabinet composite (§5.1): a cabinet store grant BUNDLES the egress allow(s) for its
-- endpoint — one propose, one terms object, one code, both halves signed in ONE
-- transaction. egress_rule_ids records the bundled nac_egress rows so revoking the
-- store tears the whole capability down (never half).
--
-- granted_to (§7/§10.2): recorded in the SIGNED terms now so delegation scoping has
-- tamper-evident data to enforce against; per-agent enforcement rides the delegation
-- plane (v064) as it wires up.

BEGIN;

ALTER TABLE dcn_stores ADD COLUMN IF NOT EXISTS note            TEXT;
ALTER TABLE dcn_stores ADD COLUMN IF NOT EXISTS proposed_by     TEXT NOT NULL DEFAULT 'host';
ALTER TABLE dcn_stores ADD COLUMN IF NOT EXISTS granted_to      JSONB NOT NULL DEFAULT '{"host": "*", "agents": ["*"]}'::jsonb;
ALTER TABLE dcn_stores ADD COLUMN IF NOT EXISTS approved_at     TIMESTAMPTZ;
ALTER TABLE dcn_stores ADD COLUMN IF NOT EXISTS approved_by     TEXT;
ALTER TABLE dcn_stores ADD COLUMN IF NOT EXISTS stepup_at       TIMESTAMPTZ;
ALTER TABLE dcn_stores ADD COLUMN IF NOT EXISTS signing_key_id  TEXT;
ALTER TABLE dcn_stores ADD COLUMN IF NOT EXISTS signed_terms    JSONB;
ALTER TABLE dcn_stores ADD COLUMN IF NOT EXISTS signature       TEXT;
ALTER TABLE dcn_stores ADD COLUMN IF NOT EXISTS expires_at      TIMESTAMPTZ;
ALTER TABLE dcn_stores ADD COLUMN IF NOT EXISTS revoked_at      TIMESTAMPTZ;
ALTER TABLE dcn_stores ADD COLUMN IF NOT EXISTS egress_rule_ids JSONB NOT NULL DEFAULT '[]'::jsonb;

COMMIT;
