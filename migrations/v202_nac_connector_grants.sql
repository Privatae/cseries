-- v202: messaging connectors — the grant, the pairing and the
-- connector capability.
--
-- A connector grant lets ONE account on ONE messaging platform start turns in
-- the person's chat, through ONE connector app. It is its own table, not a row
-- kind of nac_policy, because the two fail in opposite directions: the ingress
-- engine lets everything in when no rule exists (v058), and a connector must let
-- nothing in when no grant exists, or when this table cannot be read
-- (server/network/nac_connector.py reads it on every request, no cache).
--
-- One row is one attempt to pair an account, from the Host's draft to its
-- revocation:
--   draft      the Host proposed it (propose_nac_policy kind=connector); the
--              pairing is open and the one-time code is shown in Network only
--   matched    the connector reported the code from an account; a ticket asks
--              the person to let that account in (authenticator code)
--   approved   the terms {kind, app_id, platform, account_id} are signed with the
--              Host's entity key (signed_terms + signature); display_name sits
--              beside them unsigned, because the account can rename itself
--   revoked    declined, voided, expired, replaced by a newer approval, revoked
--              by the person or the Host, or the app was uninstalled
--
-- Only the SHA-256 of the one-time code is stored, and only while the pairing is
-- open. A failed pairing (expired, five wrong codes) keeps its row for the record.
--
-- The connector capability belongs to the APP, not to one grant: re-pairing a new
-- account keeps the capability the running connector already holds (no restart),
-- so it lives in its own row per app, again as a SHA-256 only. pending_hash is a
-- capability minted and on its way to the pod (the operator writes it into the
-- app's creds Secret on a cold wake); the kernel moves it to cap_hash only when
-- the wake answers creds_applied: true. A revoke clears both.
--
-- v199-v201 are taken.

CREATE TABLE IF NOT EXISTS nac_connector_grants (
    id              BIGSERIAL PRIMARY KEY,
    app_id          TEXT NOT NULL,
    platform        TEXT NOT NULL
                        CHECK (platform IN ('telegram', 'discord', 'slack')),
    -- The vault binding: the name the bot token is stored under in Keys. The
    -- broker injects it only into requests to this platform's API host, for this
    -- app (nac_connector.platform_call). NULL = no binding (the app cannot reach
    -- the platform through the broker).
    token_name      TEXT,
    -- Slack only: the app-level token (xapp-) Socket Mode opens its connection
    -- with (apps.connections.open). Used for that one method and nothing else.
    app_token_name  TEXT,
    -- the pairing
    pairing         TEXT NOT NULL DEFAULT 'open'
                        CHECK (pairing IN ('open', 'matched', 'void', 'expired')),
    code_hash       TEXT,
    code_expires_at TIMESTAMPTZ,
    wrong_tries     INT NOT NULL DEFAULT 0,
    -- what the connector reported alongside the matching code
    account_id      TEXT,
    display_name    TEXT,
    matched_at      TIMESTAMPTZ,
    ticket_id       BIGINT,
    -- the NAC lifecycle, as nac_policy / nac_egress / nac_mgmt_grants carry it
    proposed_by     TEXT NOT NULL DEFAULT 'host',
    proposed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    approved_at     TIMESTAMPTZ,
    approved_by     TEXT,
    stepup_at       TIMESTAMPTZ,
    signing_key_id  TEXT,
    signed_terms    JSONB,
    signature       TEXT,
    revoked_at      TIMESTAMPTZ,
    revoked_by      TEXT,
    revoke_reason   TEXT
);

-- At most one open pairing per app: starting another voids the earlier one, and
-- the index is what makes "the earlier one" unambiguous under a race.
CREATE UNIQUE INDEX IF NOT EXISTS ux_nac_connector_one_open_pairing
    ON nac_connector_grants (app_id)
    WHERE pairing = 'open' AND revoked_at IS NULL;
-- At most one live grant per app (one messaging account per connector app).
-- Approving a new account revokes the old grant first, in
-- the same transaction.
CREATE UNIQUE INDEX IF NOT EXISTS ux_nac_connector_one_live_grant
    ON nac_connector_grants (app_id)
    WHERE approved_at IS NOT NULL AND revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS ix_nac_connector_grants_app
    ON nac_connector_grants (app_id, id);

CREATE TABLE IF NOT EXISTS nac_connector_caps (
    app_id        TEXT PRIMARY KEY,
    cap_hash      TEXT,
    pending_hash  TEXT,
    pending_at    TIMESTAMPTZ,
    issued_at     TIMESTAMPTZ,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
