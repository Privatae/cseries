-- v180 — SERVICE REACH GRANTS.
--
-- A server a Host built in a bastion's service slot has no network; the Host's
-- tunnel forward is the only way in. Holding the `services` caps family on a
-- machine used to let every granted Host reach every server there. A reach grant
-- says which: Host X may reach server S on machine M until a set date.
--
-- The SAME lifecycle as a DCN store grant (v092, itself v060's shape): a draft →
-- the user approves (on the machine when it holds a second factor, else the
-- Host's own step-up) → the terms are signed with the entity key → pushed to the
-- box (service_grant_sync), which verifies and enforces the SIGNED terms on every
-- connection, default-deny. Revoke is a tighten: free, pushed (service_grant_drop)
-- and reconciled on the heartbeat.
--
-- `subject` is the name the box knows THIS Host's pairing by (the box reports it
-- as enforcing.pairing_host). The box installs a grant only from that pairing and
-- honours it only for that pairing.

BEGIN;

CREATE TABLE IF NOT EXISTS service_reach_grants (
    id              BIGSERIAL PRIMARY KEY,
    entity_id       INTEGER     NOT NULL,
    server          TEXT        NOT NULL,          -- the machine's hostname
    slot            TEXT        NOT NULL,          -- the server's slot name on it
    subject         TEXT        NOT NULL,          -- this Host's pairing name on that machine
    status          TEXT        NOT NULL DEFAULT 'draft',
    note            TEXT,
    proposed_by     TEXT        NOT NULL DEFAULT 'host',
    approved_at     TIMESTAMPTZ,
    approved_by     TEXT,
    stepup_at       TIMESTAMPTZ,
    signing_key_id  TEXT,
    signed_terms    JSONB,
    signature       TEXT,
    expires_at      TIMESTAMPTZ,
    revoked_at      TIMESTAMPTZ,
    box_request     JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT service_reach_status CHECK (status IN ('draft', 'granted', 'revoked')),
    CONSTRAINT service_reach_slot CHECK (slot ~ '^[a-z0-9][a-z0-9-]{0,30}$'),
    CONSTRAINT service_reach_fields CHECK (server <> '' AND subject <> ''
        AND position('|' in server) = 0 AND position('|' in subject) = 0)
);

-- One live grant (draft or granted) per (machine, server, host).
CREATE UNIQUE INDEX IF NOT EXISTS uq_service_reach_live
    ON service_reach_grants (entity_id, server, slot, subject)
    WHERE revoked_at IS NULL;

COMMIT;
