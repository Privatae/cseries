-- v140: sharing a machine's MODELS with another host
--
-- Someone serves a model on their own GPU and wants a friend or a
-- teammate to use it. Until now the only route was a pairing token redeemed ON
-- the machine — a terminal, or the box's local setup page — so "share my GPU"
-- meant letting someone touch the box, or going and touching it yourself. And
-- the narrowest thing the anchor could then grant was the whole `inference`
-- family: every loaded model, the OpenAI-compatible server beside it, and the
-- PII sterilizer.
--
-- The hard stop, in his words: SYSTEM RESOURCES ONLY, NOT DATA. No folder, no
-- index, no context store. The other host must be able to say truthfully "the
-- only thing we can do with this machine is use the qwen4 model."
--
-- TWO CHANGES.
--
-- 1. nac_pairing_grants.models — the model scope, alongside caps. It is a
--    ceiling INSIDE the inference family rather than a family of its own.
--    '[]' means UNSCOPED, which is exactly what every grant signed before
--    today means, so the default rewrites nothing. The list also travels in
--    the SIGNED terms (pairing_grants.grant_terms omits the key when empty, so
--    an unscoped grant canonicalizes to the same bytes it always did), and the
--    machine is the party that enforces it: the gateway's client
--    (modelscope.go + inferencefilter.go) checks the model each request names and
--    filter the model lists on the way back. This column is what the host
--    SIGNED, for display and for audit — never an authorization input here.
--
-- 2. network_machine_offers — the ask, both directions.
--      'in'  someone asked to use one of our machines; the user decides.
--      'out' we asked to use one of theirs; we are waiting.
--    An open OUTBOUND row is also an authorization: while it stands, that peer
--    may ask this host to mint ONE bastion enrolment token (peer op
--    machine.token). That is the mechanism that lets their approval complete
--    without anyone typing on a machine, and it is why the row is bounded —
--    tokens_issued and last_token_at cap the count and the rate, and the
--    partial unique index allows only one open row per peer per direction.
--
-- NO TOKEN IS STORED. A pairing token lives 15 minutes and is single-use, so
-- one minted when an offer is SENT would be dead before a human answered. It
-- is fetched at the moment of approval, over the direct host-to-host lane
-- (server/peer), used within seconds, and never written down. It never rides
-- the platform relay: that path reads and rewrites message bodies in cleartext
-- (bridge adapter.py _enrich_and_verify_cross_host) and drops between its own
-- DaemonSet pods.
--
-- Idempotent; mirrored defensively in pairing_grants.ensure_tables and
-- machine_offers.ensure_tables.

BEGIN;

ALTER TABLE nac_pairing_grants
    ADD COLUMN IF NOT EXISTS models JSONB NOT NULL DEFAULT '[]';

CREATE TABLE IF NOT EXISTS network_machine_offers (
    id               BIGSERIAL PRIMARY KEY,
    direction        TEXT NOT NULL,          -- 'in' (they asked) | 'out' (we asked)
    peer_entity_id   INTEGER NOT NULL,
    peer_handle      TEXT,
    peer_display     TEXT,
    purpose          TEXT,                   -- their words, sanitised + capped
    models_asked     JSONB NOT NULL DEFAULT '[]',  -- a HINT on the card, never a grant
    state            TEXT NOT NULL DEFAULT 'open', -- open | accepted | declined | withdrawn
    server           TEXT,                   -- the machine, once the user picks one
    subject          TEXT,                   -- the host_name the box redeemed
    granted_caps     JSONB,
    granted_models   JSONB,
    tokens_issued    INTEGER NOT NULL DEFAULT 0,
    last_token_at    TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_at       TIMESTAMPTZ,
    decided_by       TEXT,
    detail           TEXT
);

-- One OPEN offer per peer per direction. In the schema rather than in a
-- count-then-insert, because two connections can both win that race — and on
-- the outbound side losing it would mean two standing minting rights.
CREATE UNIQUE INDEX IF NOT EXISTS ux_machine_offers_open
    ON network_machine_offers (direction, peer_entity_id)
 WHERE state = 'open';

COMMIT;
