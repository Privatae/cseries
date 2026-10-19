-- v170 — what another Host shared about itself on an accepted connection.
--
-- Name and number are shared on accept. The contact card
-- (server/hosts/card.py) shows another Host's owner and contact number; this
-- Host learns both only from that Host, and only once one of the two accepted
-- the other (server/hosts/peer_cards.py). The number is kept only after the
-- platform confirmed it belongs to the Host that sent it.
CREATE TABLE IF NOT EXISTS peer_cards (
    peer_entity_id     INTEGER      PRIMARY KEY,
    owner_name         TEXT,
    phone              TEXT,
    phone_verified_at  TIMESTAMPTZ,
    updated_at         TIMESTAMPTZ  NOT NULL DEFAULT now()
);
