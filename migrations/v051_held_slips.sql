-- v051 (2026-06-16): held_slips — a host stores a permission slip that a PEER
-- issued it (via the bridge contract.issue, delivered nested in an endpoint_grant),
-- so it can PRESENT that slip when it later messages the peer. The bridge then
-- verifies the slip and the peer's receiver honors it (authorized_via_slip) instead
-- of falling back to the domain_policy allow-list.
--
-- Pod-local: each host holds only the slips peers granted IT. Keyed by the peer's
-- entity id because the DM send path targets an entity id. The slip value is the
-- bridge-signed capability {terms, signature, ...} verbatim — we just hold it.
CREATE TABLE IF NOT EXISTS held_slips (
    peer_entity_id  INTEGER PRIMARY KEY,
    slip            JSONB        NOT NULL,
    contract_id     TEXT,
    expires_at      TIMESTAMPTZ,
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT now()
);
