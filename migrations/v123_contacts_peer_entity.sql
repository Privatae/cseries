-- v123_contacts_peer_entity.sql — Circles: Hosts | People (2026-09-02)
--
-- A contact now remembers WHICH HOST it is reached through. For a host contact
-- that is the host itself; for a human contact (kind='human', handle = the
-- person's account email) it is the host the person runs — the endpoint every
-- message to that person is delivered to. Without it the People send path had
-- no way to find the permission slip the peer host issued us (held_slips is
-- keyed by peer entity id) and no way to address the delivery.
--
-- Filled by: accept-request (the requester's id rides on the endpoint_request),
-- and by the endpoint_grant receiver (the acceptor's id rides on the grant).
-- Existing rows stay NULL until the next exchange with that contact; the
-- Circles list derives peers from messages, so nothing goes dark meanwhile.
--
-- Idempotent, forward-only.

ALTER TABLE contacts ADD COLUMN IF NOT EXISTS peer_entity_id INTEGER;

CREATE INDEX IF NOT EXISTS idx_contacts_peer_entity
    ON contacts (peer_entity_id) WHERE peer_entity_id IS NOT NULL;
