-- v153: the peer lane carries APPS.
--
-- Before this, every op on the direct host<->host lane (:8444) was host-level:
-- a knock, its grant, a direct message, a machine token. An installed app had
-- no way to reach the same app on a peer's host. These three tables are what
-- lets an app ride the lane without ever touching a cert or a slip itself.
--
-- DDL lives here and nowhere else: CREATE TABLE IF NOT EXISTS /
-- CREATE INDEX IF NOT EXISTS only, so this is a no-op on a host that already
-- has the tables and the full shape on an empty one.

-- ── Slips a PEER issued US, scoped to one app. ─────────────────────────────
-- held_slips (v051) is keyed by peer alone because the DM lane needs exactly
-- one slip per peer. An app slip is keyed by (peer, app): the same peer may
-- grant several apps, and a slip for app A must never be presentable as app B.
CREATE TABLE IF NOT EXISTS peer_app_slips (
    peer_entity_id  INTEGER      NOT NULL,
    app_id          TEXT         NOT NULL,
    slip            JSONB        NOT NULL,
    contract_id     TEXT,
    expires_at      TIMESTAMPTZ,
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (peer_entity_id, app_id)
);

COMMENT ON TABLE peer_app_slips IS
    'A permission slip a PEER host issued this host for ONE app (allowed_ops '
    'carries app:<app_id>). Presented on app.deliver to that peer; verified '
    'offline by the peer exactly like a DM slip. Renewed on use at half-life '
    'under the same contract_id.';

-- ── Outbox — store-and-forward, the sender side. ───────────────────────────
-- The lane is one request/response per connection and a peer is often offline
-- (peer_client raises PeerUnavailable). An app must not have to write its own
-- retry loop, so every send lands here first and a loop drives it.
CREATE TABLE IF NOT EXISTS peer_app_outbox (
    id               BIGSERIAL    PRIMARY KEY,
    app_id           TEXT         NOT NULL,
    peer_entity_id   INTEGER      NOT NULL,
    op               TEXT         NOT NULL,
    action           TEXT         NOT NULL DEFAULT '',
    payload          JSONB        NOT NULL DEFAULT '{}'::jsonb,
    message_id       UUID         NOT NULL,
    status           TEXT         NOT NULL DEFAULT 'queued',
    attempts         INTEGER      NOT NULL DEFAULT 0,
    next_attempt_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    last_error       TEXT,
    response         JSONB,
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),
    delivered_at     TIMESTAMPTZ,
    expires_at       TIMESTAMPTZ  NOT NULL DEFAULT (now() + interval '7 days'),
    -- Idempotency is PER (app, peer): one logical message fanned out to two
    -- peers under one id is two rows, and two apps never share a row. A bare
    -- UNIQUE (message_id) would make the second fan-out write onto the first
    -- row.
    UNIQUE (app_id, peer_entity_id, message_id)
);

COMMENT ON TABLE peer_app_outbox IS
    'Every outbound app send, written BEFORE the first attempt. op is the wire '
    'op (app.deliver | app.grant). status: queued | delivered | refused | expired. '
    'A transport failure is retried with backoff until expires_at; a refusal '
    'is the far side''s authoritative answer and is never retried.';

CREATE INDEX IF NOT EXISTS peer_app_outbox_due_idx
    ON peer_app_outbox (next_attempt_at) WHERE status = 'queued';
CREATE INDEX IF NOT EXISTS peer_app_outbox_app_idx
    ON peer_app_outbox (app_id, created_at DESC);

-- ── Inbox — the receiver side. ─────────────────────────────────────────────
-- A delivery is stored BEFORE any attempt to hand it to a live app process, so
-- an app that is not running (or is static, and has no process at all) still
-- receives everything sent to it. The app reads and acks through /api/peer/app.
CREATE TABLE IF NOT EXISTS peer_app_inbox (
    id               BIGSERIAL    PRIMARY KEY,
    app_id           TEXT         NOT NULL,
    peer_entity_id   INTEGER      NOT NULL,
    action           TEXT         NOT NULL,
    payload          JSONB        NOT NULL DEFAULT '{}'::jsonb,
    message_id       UUID         NOT NULL,
    received_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    acked_at         TIMESTAMPTZ,
    -- Per (app, sender): peer A's message id must never consume peer B's.
    UNIQUE (app_id, peer_entity_id, message_id)
);

COMMENT ON TABLE peer_app_inbox IS
    'One row per app.deliver this host accepted, UNIQUE on (app, sender, the '
    'sender''s message_id) so the outbox''s retries are idempotent and one '
    'peer cannot consume another''s ids. peer_entity_id is the client-cert '
    'identity, never a field the sender chose. acked_at is set by the app; '
    'rows are removed only when the app is uninstalled.';

CREATE INDEX IF NOT EXISTS peer_app_inbox_app_unacked_idx
    ON peer_app_inbox (app_id, id) WHERE acked_at IS NULL;
