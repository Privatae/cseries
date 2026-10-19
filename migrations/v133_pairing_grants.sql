-- v133: anchored bastions — mode, anchor, pairings, and the PAIRING GRANT (2026-09-08)
--
-- Companion to the gateway's anchor-pairing-grants change (bastion side).
--
-- WHAT CHANGED ON THE BOX. A bastion now reports which of two modes it runs in
-- ('local': one bearer, one host; 'networked': many hosts, root-capable ops), who
-- anchored it (a root-proved claim bound to ONE host), every pairing it holds with
-- its state (active / pending / revoked), and the pairings nobody approved. On an
-- anchored machine every pairing after the anchor is PENDING until the anchor
-- host's user approves it (every new pairing on an anchored machine needs the
-- anchor host's approval), because same-user cannot be verified
-- without an identity authority and one approval per machine is cheap.
--
-- The approval is a PAIRING GRANT: terms signed by the anchor host's entity_sign
-- key, exactly as a DCN store grant is (same canonical JSON, same key,
-- nac_stepup.sign_policy_terms), delivered over the anchor's own mgmt lane
-- (pairing_grant_sync). The box verifies against the anchor key it pinned at
-- claim-anchor and flips the pairing ACTIVE. Revocation is one signed op
-- (pairing_grant_drop) that tears the peer's lanes down at once.
--
-- Nothing here is an authorization input on THIS host. The heartbeat columns are
-- the box's word about itself; the grants table is what this host signed. The
-- only party that enforces a pairing grant is the machine it names.
--
-- Idempotent; mirrors the defensive ADD COLUMN / CREATE in topology.ensure_table
-- and pairing_grants.ensure_tables.

BEGIN;

-- The heartbeat side: four nullable columns, NO defaults. A bastion that predates
-- 0.15 sends none of them, and (as with peers/owner_claim in v132) absence must
-- stay distinguishable from an explicit answer: NULL mode = "too old to say",
-- never "local"; NULL pairings = "never reported", never "holds none".
ALTER TABLE network_bastion_heartbeat
    ADD COLUMN IF NOT EXISTS mode TEXT;
ALTER TABLE network_bastion_heartbeat
    ADD COLUMN IF NOT EXISTS anchor JSONB;
ALTER TABLE network_bastion_heartbeat
    ADD COLUMN IF NOT EXISTS pairings JSONB;
ALTER TABLE network_bastion_heartbeat
    ADD COLUMN IF NOT EXISTS unapproved_pairing_attempts JSONB;

COMMENT ON COLUMN network_bastion_heartbeat.mode IS
    'local | networked, as the box reports it. NULL = a bastion too old to say.';
COMMENT ON COLUMN network_bastion_heartbeat.anchor IS
    'The root-proved anchor claim {owner, anchor_host, anchor_handle, claimed_at, '
    'public_key, signature}. Trusted by that machine alone, for one decision: who '
    'may pair next. NULL = not anchored (or too old to say).';
COMMENT ON COLUMN network_bastion_heartbeat.pairings IS
    'Every pairing the box holds: [{host_name, handle, state, since}], state in '
    'active|pending|revoked. The box''s word; authorizes nothing here.';
COMMENT ON COLUMN network_bastion_heartbeat.unapproved_pairing_attempts IS
    'Pairings still pending with no grant: [{host_name, handle, since}]. Reported '
    'on every beat until removed — this is the intrusion signal.';

-- The durable side of "have we told the user" — same reason network_bastion_peer
-- exists (v132): the heartbeat column is overwritten every beat, so an in-memory
-- diff loses the introduction on any retry, restart or two pods racing one beat.
-- One row per (machine, subject host). Rows are never deleted: a pairing that
-- was denied, or that stopped being announced, is still something the user did.
CREATE TABLE IF NOT EXISTS network_bastion_pairing (
    token_id           INTEGER NOT NULL,
    subject            TEXT    NOT NULL,   -- the subject host_name
    subject_handle     TEXT,
    state              TEXT,               -- last reported: active | pending | revoked
    since              TEXT,               -- the box's word (pairing file mtime), may be empty
    first_seen_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Set only on a CONFIRMED inbox write (handle_inbox_append returns {"ok":
    -- false} rather than raising). Presence = the user was asked ONCE about this
    -- pending pairing.
    pending_notified_at  TIMESTAMPTZ,
    -- An attempt older than 10 minutes with no answer is raised again, but at
    -- most once per day: this is the timestamp of the last raise.
    unapproved_raised_at TIMESTAMPTZ,
    -- The user said no. Nothing is sent to the box (the pairing stays pending
    -- there and keeps being reported); this stops the daily reminder and marks
    -- the card. A later approve clears it.
    denied_at          TIMESTAMPTZ,
    denied_by          TEXT,
    PRIMARY KEY (token_id, subject)
);

-- The grants THIS host signed as anchor. Same shape as nac_mgmt_grants (v060):
-- signed terms + signature + expiry + revoke, approved_by + stepup_at for the
-- audit trail. `terms_canonical` is the EXACT bytes that were signed (base64),
-- because the box verifies over those bytes and never re-canonicalizes
-- (Python and Go canonical JSON differ on unicode; byte identity sidesteps it).
CREATE TABLE IF NOT EXISTS nac_pairing_grants (
    id              BIGSERIAL PRIMARY KEY,
    entity_id       INTEGER,
    server          TEXT NOT NULL,          -- the bastion hostname the grant is for
    subject         TEXT NOT NULL,          -- the subject host_name being admitted
    subject_handle  TEXT,
    caps            JSONB NOT NULL DEFAULT '[]',  -- ceiling: subset of observe,session,dcn,harness
    terms_canonical TEXT NOT NULL,          -- base64 of the signed canonical JSON
    signature       TEXT NOT NULL,          -- base64 ed25519 under entity_sign
    issued_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at      TIMESTAMPTZ,
    revoked_at      TIMESTAMPTZ,
    revoke_terms_canonical TEXT,            -- the signed pairing_revoke terms, once revoked
    revoke_signature TEXT,
    approved_by     TEXT,
    stepup_at       TIMESTAMPTZ,
    -- Delivery state: the box must hold the grant before the pairing goes active.
    -- NULL = not delivered yet (box offline at approval); the heartbeat path
    -- retries while the box still reports the pairing as pending.
    synced_at       TIMESTAMPTZ,
    sync_detail     TEXT
);

CREATE INDEX IF NOT EXISTS nac_pairing_grants_server_subject
    ON nac_pairing_grants (server, subject) WHERE revoked_at IS NULL;

COMMIT;
