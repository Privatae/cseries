-- v132: a bastion announces the OTHER hosts it serves
--
-- Numbered 132, not 130: v130 and v131 are taken. Two migrations sharing a
-- number would give two entities
-- different schemas depending on which one ran.
--
-- One bastion daemon serves many hosts — each pairing its own file, its own
-- keys, its own context and DCN state. That isolation is deliberate, and it
-- means the box is the ONLY party that knows the set: every pairing mints a
-- fresh WireGuard keypair, and the device "fingerprint" is fresh random bytes
-- per pairing, so two pairings of one machine cannot be linked from outside.
-- The platform therefore cannot derive peers, and should not be able to —
-- correlating them platform-side would mean silently linking a person's work
-- host to their personal one.
--
-- So discovery is the machine ANNOUNCING, on the heartbeat it already sends.
-- Automatic and symmetric — pairing IS the consent. Pair a
-- bastion to a second host and both hosts learn of each other on the next beat,
-- which is what lets an IT team's hosts assemble themselves with no steps, and
-- is also how you find out someone paired a host to a machine you own.
--
-- WHAT THIS COLUMN HOLDS: [{host_name, handle, since}] and nothing else. Not
-- keys, not grants, not context, not fingerprints. The announcement is the
-- bastion's CLAIM and authorizes nothing — no reach, no slip, no grant; a
-- secured channel still needs its bridge-signed permission slip. Narrowed at
-- the boundary in topology.upsert_heartbeat before it is ever written here.
--
-- NULL vs '[]' IS LOAD-BEARING, which is why there is no DEFAULT:
--   NULL = this bastion has never reported peers (it predates 0.14.0). The
--          upsert COALESCEs, so such a beat never retracts a stored set.
--   '[]' = the box says it serves only this host now — a real state change.
-- A DEFAULT '[]' would make every old bastion assert "I serve one host" and
-- silently drop peers the user can see today.
--
-- Idempotent; mirrors the defensive ADD COLUMN in topology.ensure_table.

BEGIN;

ALTER TABLE network_bastion_heartbeat
    ADD COLUMN IF NOT EXISTS peers JSONB;

-- Machine ownership: the box's optional root-signed owner claim
-- {owner, claimed_at, public_key, signature}. It proves
-- CONTROL of the machine rather than binding to an organisation identity: whoever can
-- run root on the box IS the owner, and that is the only true statement
-- available on a network with no directory to appeal to.
--
-- It answers "whose box is this", and nothing more. Nothing gates on it; the
-- moment something does, it becomes a credential any root user can mint for
-- themselves. NULL = nobody has said, which is normal — the inspector simply
-- shows no owner line. No DEFAULT, for the same reason as peers: absence must
-- stay distinguishable from an explicit answer.
ALTER TABLE network_bastion_heartbeat
    ADD COLUMN IF NOT EXISTS owner_claim JSONB;

COMMENT ON COLUMN network_bastion_heartbeat.owner_claim IS
    'Who owns this machine, as stated by someone with root on it '
    '{owner, claimed_at, public_key, signature}. Displayed as a plain name. '
    'Never an authorization input. NULL = nobody has said.';

-- The durable side: one row per (machine, peer), which is what dedupes the
-- user-facing introduction across beats, restarts, and two pods racing one
-- heartbeat. An in-memory diff of the JSONB column above cannot do that — the
-- same statement that would supply the "before" also overwrites it.
--
-- A peering is never deleted. When a bastion stops announcing it,
-- last_seen_at simply stops moving and the row reads dormant: "a machine I
-- used to share with Acme" is exactly the awareness this exists to give.
CREATE TABLE IF NOT EXISTS network_bastion_peer (
    token_id       INTEGER NOT NULL,
    peer_host_name TEXT    NOT NULL,
    peer_handle    TEXT,
    peer_since     TEXT,
    -- Resolved once from the handle over the bridge. Circles keys a host
    -- thread by entity id, so a peering without one can be listed but never
    -- opened; NULL is retried on later beats rather than being an error.
    peer_entity_id INTEGER,
    first_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Set only on a CONFIRMED inbox write. handle_inbox_append returns
    -- {"ok": false} rather than raising, so setting this on a bare await would
    -- record a failed notification as delivered and the dedupe would guarantee
    -- the user was never told.
    notified_at    TIMESTAMPTZ,
    PRIMARY KEY (token_id, peer_host_name)
);

ALTER TABLE network_bastion_peer
    ADD COLUMN IF NOT EXISTS peer_entity_id INTEGER;

COMMENT ON COLUMN network_bastion_heartbeat.peers IS
    'Other hosts this bastion serves, as the box reports them: '
    '[{host_name, handle, since}]. A claim, not proof; authorizes nothing. '
    'NULL = never reported (pre-0.14.0 bastion); [] = serves only this host.';

COMMIT;
