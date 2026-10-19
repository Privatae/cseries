-- v120 (2026-08-29): DCN MOVE PLANS — the one write lane on a machine store,
-- and it is ARCHIVE-ONLY.
--
-- The host reads the FreeAgent's catalog (metadata; no file leaves the
-- machine), proposes a new structure as a plan of `move` operations inside the
-- granted root ("remove" = move into an archive folder), and the USER approves
-- it with their step-up code. The approved terms are signed with the host's
-- entity key; the bastion (the gateway's dcn_plan_apply) verifies the signature
-- + expiry against its pinned key, checks every move against the sandbox
-- (dry_run), renames in order with rollback on failure, verifies, and reports.
-- No delete op exists anywhere on this lane; a target that already exists is a
-- refusal, never an overwrite.
--
-- Same lifecycle shape as dcn_stores (v092): draft → approved (signed) →
-- applied | failed; revoke is free. expires_at is SHORT (24h): a plan is
-- executed right after approval, not held as standing authorization.
-- The runtime ensures the table defensively (nac_dcn.ensure_plans_table);
-- this is the authoritative record.
CREATE TABLE IF NOT EXISTS dcn_move_plans (
    id              BIGSERIAL PRIMARY KEY,
    entity_id       INTEGER NOT NULL,
    store_id        BIGINT NOT NULL,                      -- dcn_stores.id (machine class)
    title           TEXT NOT NULL,
    note            TEXT,
    archive         TEXT NOT NULL,                        -- relative folder under the root that "remove" moves into
    moves           JSONB NOT NULL DEFAULT '[]'::jsonb,   -- [{from, to, size_bytes?}, ...] relative to the root
    move_count      INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'draft'
                        CHECK (status IN ('draft','approved','applied','failed','revoked')),
    proposed_by     TEXT NOT NULL DEFAULT 'host',
    approved_at     TIMESTAMPTZ,
    approved_by     TEXT,
    stepup_at       TIMESTAMPTZ,
    signing_key_id  TEXT,
    signed_terms    JSONB,
    signature       TEXT,
    expires_at      TIMESTAMPTZ,
    revoked_at      TIMESTAMPTZ,
    dry_run_at      TIMESTAMPTZ,
    dry_run_report  JSONB,
    applied_at      TIMESTAMPTZ,
    report          JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_dcn_move_plans_store ON dcn_move_plans (entity_id, store_id);
