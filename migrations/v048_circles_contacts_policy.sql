-- v048 (2026-06-11): Circles upgrade — contacts (address book) + domain_policy
-- (who can reach this container) tables.
--
-- Backs the previously-dead Contacts and Policy rail tabs in the Circles app.
-- The app.yaml has declared read/write:address_book and read/write:domain_policy
-- scopes since v0.1 (system-apps/circles/app.yaml:40-43) but no table or route
-- ever existed. These are pod-local: each entity owns
-- its own address book and reachability policy — the platform never holds them.

CREATE TABLE IF NOT EXISTS contacts (
    id              SERIAL PRIMARY KEY,
    -- The peer's synaptive handle (host contacts) or inbox address
    -- (human contacts, e.g. someone@user.example).
    handle          TEXT NOT NULL,
    display_name    TEXT,
    kind            TEXT NOT NULL DEFAULT 'host'
                    CHECK (kind IN ('host', 'human')),
    notes           TEXT,
    -- active   = normal contact
    -- pending  = we accepted an endpoint_request / sent one, grant not confirmed
    -- blocked  = explicit deny; also mirrored as a domain_policy rule
    status          TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active', 'pending', 'blocked')),
    last_contact_at TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_contacts_handle ON contacts(handle);

-- Inbound-reachability policy. peer='*' is the default-posture row; specific
-- peers (handle or full inbox address) override it. Enforced pod-side in the
-- inbox append path — the pod decides who reaches it, not the platform.
CREATE TABLE IF NOT EXISTS domain_policy (
    id                  SERIAL PRIMARY KEY,
    peer                TEXT NOT NULL,
    allow_inbox         BOOLEAN NOT NULL DEFAULT TRUE,
    allow_dm            BOOLEAN NOT NULL DEFAULT TRUE,
    allow_circle_invite BOOLEAN NOT NULL DEFAULT TRUE,
    note                TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_domain_policy_peer ON domain_policy(peer);

-- Default posture: open. Platform notifications (cseries@localhost) and
-- self-sends always pass regardless of posture; the row governs cross-entity
-- traffic once it exists.
INSERT INTO domain_policy (peer, allow_inbox, allow_dm, allow_circle_invite, note)
VALUES ('*', TRUE, TRUE, TRUE, 'default posture — open')
ON CONFLICT (peer) DO NOTHING;
