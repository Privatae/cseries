-- v154: groups — a roster and a signed vote.
--
-- A group is a roster plus a signed vote. These tables are the CREATOR-HELD
-- half: the roster of a private group circle lives in its creator's own pod
-- and is replicated to members over the peer lane. The platform holds nothing
-- for these groups. (The community tier's roster lives in communityd — same
-- shape, different owner.)
--
-- DDL lives here and nowhere else: CREATE TABLE IF NOT EXISTS /
-- CREATE INDEX IF NOT EXISTS only, so this is a no-op on a host that already
-- has the tables and the full shape on an empty one.
--
-- EXISTING PLATFORM-HELD CIRCLE ROSTERS ARE NOT MIGRATED. circle_whitelist on
-- the platform keeps working exactly as it does; group circles start fresh
-- under this shape. The two are different objects that share a word: one is a
-- platform-held whitelist keyed on a platform circle id, the other a host-held
-- roster keyed on a host-minted group id and carrying a public key. There is
-- no backfill, no dual write and no reconciler between them.

-- ── The group. ─────────────────────────────────────────────────────────────
-- group_id is a UUID text minted by the CREATOR's host, so it is globally
-- unique with no registry and no allocation round-trip — the same reason the
-- bridge mints contract_id as "slip-" + uuid4().hex.
--
-- The creator's row and a member's REPLICA live in this same table. A row
-- whose owner_entity_id is this host is authoritative; any other row is a
-- replica refreshed by group.roster_fetch and never written by a local route.
-- One shape means one set of queries and one tally function; a separate
-- replica table would have doubled every read path to buy nothing.
CREATE TABLE IF NOT EXISTS groups (
    group_id         TEXT         PRIMARY KEY,
    owner_entity_id  INTEGER      NOT NULL,
    name             TEXT         NOT NULL,
    description      TEXT         NOT NULL DEFAULT '',
    -- Bumped on every membership or role change. A replica stores the epoch it
    -- last pulled and the owner returns the current one on every answer, so a
    -- member knows it is behind without diffing the roster.
    roster_epoch     BIGINT       NOT NULL DEFAULT 1,
    -- What this host last successfully pulled. NULL on the owner's own row.
    synced_epoch     BIGINT,
    synced_at        TIMESTAMPTZ,
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),
    revoked_at       TIMESTAMPTZ
);

COMMENT ON TABLE groups IS
    'A group circle. owner_entity_id = the creator''s host, which holds the '
    'authoritative roster. A row owned by another entity is a replica pulled '
    'over the peer lane. roster_epoch is monotonic per group and bumps on '
    'every membership or role change.';

CREATE INDEX IF NOT EXISTS groups_owner_idx ON groups (owner_entity_id);

-- ── The roster. ────────────────────────────────────────────────────────────
-- verify_pubkey is the member's raw 32-byte Ed25519 public key — the host's
-- entity_sign key (server/applications/entity_signing.py), the same key it
-- signs NAC policy and pairing grants with. Carrying it HERE is what makes a
-- tally recountable: anyone holding the roster can verify every vote in the
-- group without asking anybody anything.
--
-- The pin is not self-asserted. The key arrives on a peer-lane call whose
-- caller identity came from the mTLS client-cert CN before the handler ran
-- (server/peer/peer_server.py:123-125, :242), so it is written against the
-- cert's entity id and never against a field in the request body.
--
-- FIRST KEY WINS. A different key later is refused and reported rather than
-- overwritten, the way arm_hub_pdp treats a changed key as an alarm
-- (server/network/hub_peers.py:120-135). A genuine key change is a re-join.
--
-- A removed member is removed_at-stamped, NEVER deleted. Their votes stay
-- attributable, so a tally over a closed ballot can say who was on the roster
-- when it closed rather than who is on it now. Deleting the row would make
-- every historical signature unverifiable.
CREATE TABLE IF NOT EXISTS group_members (
    group_id         TEXT         NOT NULL,
    member_entity_id INTEGER      NOT NULL,
    role             TEXT         NOT NULL DEFAULT 'member'
                     CHECK (role IN ('creator', 'admin', 'moderator', 'member')),
    verify_pubkey    BYTEA,
    key_pinned_at    TIMESTAMPTZ,
    display_name     TEXT         NOT NULL DEFAULT '',
    added_at         TIMESTAMPTZ  NOT NULL DEFAULT now(),
    removed_at       TIMESTAMPTZ,
    PRIMARY KEY (group_id, member_entity_id)
);

COMMENT ON TABLE group_members IS
    'One row per member per group. verify_pubkey is the member host''s raw '
    '32-byte Ed25519 entity_sign public key, pinned first-key-wins against the '
    'peer-lane cert identity. A removed member is removed_at-stamped so past '
    'votes stay attributable.';

CREATE INDEX IF NOT EXISTS group_members_current_idx
    ON group_members (group_id, member_entity_id) WHERE removed_at IS NULL;
CREATE INDEX IF NOT EXISTS group_members_entity_idx
    ON group_members (member_entity_id) WHERE removed_at IS NULL;

-- ── The ballot — the thing voted on. ───────────────────────────────────────
-- closes_at is REQUIRED and bounded at 366 days by the route, so a ballot can
-- never outlive the relationship term that carries its votes (the bridge's own
-- _CONTACT_TERM_MAX).
CREATE TABLE IF NOT EXISTS group_ballots (
    group_id     TEXT         NOT NULL,
    ballot_id    TEXT         NOT NULL,
    question     TEXT         NOT NULL,
    -- The declared choices. A vote naming anything else is refused, so this
    -- list is the whole vocabulary of the ballot.
    choices      JSONB        NOT NULL,
    -- What the ballot is ABOUT, for the caller's own bookkeeping: the group
    -- layer never acts on a result, so these are labels, not references the
    -- code follows.
    target_kind  TEXT         NOT NULL DEFAULT '',
    target_ref   TEXT         NOT NULL DEFAULT '',
    created_by   INTEGER      NOT NULL,
    opens_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
    closes_at    TIMESTAMPTZ  NOT NULL,
    closed_at    TIMESTAMPTZ,
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (group_id, ballot_id)
);

COMMENT ON TABLE group_ballots IS
    'One question put to a group. Votes are advisory: nothing in the group '
    'layer executes on a count. Carried forward from the board''s design of '
    'record — no auto-approval of anything via votes.';

-- ── The signed vote. ───────────────────────────────────────────────────────
-- ONE ROW PER (group, ballot, voter). The row is the vote; a change of mind is
-- a higher seq replacing it in place, which is why seq is stored and not just
-- checked.
--
-- WHAT THE SIGNATURE PROVES, stated here because the table is where someone
-- will reach for it: a host said this. NOT that a person did. The entity's own
-- workload can read entity_sign out of kernel_secrets and sign what it likes
-- as itself (server/network/nac_stepup.py:15-21 states this limit for the same
-- key). What it buys is that a tallier cannot invent a vote for a member it
-- cannot sign for, cannot silently drop one the member can present, and that a
-- vote keeps its author across every relay. One host, one vote. It is not
-- Sybil resistance and must never be described as it.
--
-- verified is stored rather than recomputed on read so a row that arrived
-- before its member's key was pinned is visibly uncounted instead of silently
-- absent; verify_error says which of the checks it failed.
CREATE TABLE IF NOT EXISTS group_votes (
    group_id        TEXT         NOT NULL,
    ballot_id       TEXT         NOT NULL,
    voter_entity_id INTEGER      NOT NULL,
    choice          TEXT         NOT NULL,
    -- Monotonic per (group, voter). A vote at or below the highest seq already
    -- stored for that voter is refused, so a change of mind is expressible and
    -- a replay of an older signed vote is not.
    seq             BIGINT       NOT NULL,
    nonce           TEXT         NOT NULL,
    signed_at       TIMESTAMPTZ  NOT NULL,
    signature       BYTEA        NOT NULL,
    key_id          TEXT         NOT NULL DEFAULT 'entity_sign',
    verified        BOOLEAN      NOT NULL DEFAULT FALSE,
    verify_error    TEXT,
    received_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (group_id, ballot_id, voter_entity_id)
);

COMMENT ON TABLE group_votes IS
    'One signed vote per (group, ballot, voter). signature is Ed25519 over the '
    'canonical JSON of {v, kind, group, ballot, voter, choice, seq, nonce, '
    'signed_at} — the same canonical form and the same entity_sign key the '
    'host signs NAC policy and pairing grants with. A tally is a count of '
    'valid signatures against the roster, recomputable by anyone holding it.';

CREATE INDEX IF NOT EXISTS group_votes_ballot_idx
    ON group_votes (group_id, ballot_id);

-- ── Which apps a group carries, on the peer lane. ──────────────────────────
-- The group app grant: one user decision per host instead of one
-- per pair. A row here means "every current member of this group may be
-- granted this app", and the roster reconciler fans a grant out to members as
-- they join and stops for members who leave.
--
-- A NEW peer_app_outbox STATUS, declared here because v153 is shipped and a
-- past migration is never edited. `peer_app_outbox.status` gains 'revoked'
-- beside queued | delivered | refused | expired (the four v153's comment
-- lists). The column is plain TEXT with no CHECK, so this needs no DDL — but a
-- reader of v153 would otherwise never learn a fifth value exists.
--
-- It means: this grant was issued and then withdrawn, because the member left
-- the group. It is what makes RE-ADMISSION work. The reconciler counts a grant
-- only while it is LIVE (server/peer/app_lane.py _LIVE_GROUP_GRANT), so
-- without the stamp a member removed and added back is skipped for ever: their
-- slip is revoked and the stale `delivered` row still matches. 'refused' could
-- not be reused for it — that means the far side said no, and this is our own
-- withdrawal.
CREATE TABLE IF NOT EXISTS group_app_grants (
    group_id    TEXT         NOT NULL,
    app_id      TEXT         NOT NULL,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    revoked_at  TIMESTAMPTZ,
    PRIMARY KEY (group_id, app_id)
);

COMMENT ON TABLE group_app_grants IS
    'An app this host grants to a whole group rather than to one contact. The '
    'slip minted for each member names resource group:<id>:app:<app_id>, so '
    'the group binding is inside the bridge-signed terms; allowed_ops is '
    'unchanged (app:<app_id>) and neither the bridge nor verify_slip changes.';

-- ── The group slip a member holds from an owner. ───────────────────────────
-- The credential a member presents on group.roster_fetch and group.vote. It is
-- the owner's grant, so it is keyed by group alone: one group, one owner, one
-- slip. Renewed at half-life under the same contract_id, exactly as a contact
-- slip and an app slip are (server/peer/app_lane.py:314-354).
--
-- owner_entity_id is stored beside it because the slip is the thing that says
-- WHERE to present it: a member learns a group's owner from the grant that
-- carried the slip, and never has to resolve a group id to a host.
CREATE TABLE IF NOT EXISTS group_slips (
    group_id         TEXT         PRIMARY KEY,
    owner_entity_id  INTEGER      NOT NULL,
    slip             JSONB        NOT NULL,
    contract_id      TEXT,
    expires_at       TIMESTAMPTZ,
    updated_at       TIMESTAMPTZ  NOT NULL DEFAULT now()
);

COMMENT ON TABLE group_slips IS
    'A permission slip a group OWNER issued this host for one group '
    '(allowed_ops carries group:<group_id>). Presented on group.roster_fetch '
    'and group.vote; verified offline by the owner exactly like a DM slip.';

-- ── Outbox — store-and-forward for the group lane. ─────────────────────────
-- Same doctrine as peer_app_outbox and the SAME decision code: the group lane
-- imports app_lane.classify and app_lane._backoff_s rather than restating
-- them. A separate TABLE, not a separate doctrine, and the reason is a
-- contract: peer_app_outbox is keyed (app_id, peer_entity_id, message_id) with
-- app_id NOT NULL, and every row reaching app_lane._send is sent for an app
-- that must be installed and must have declared read:peer_app
-- (server/peer/app_lane.py:232-247). A group op names no app. Riding that
-- table would mean inventing a fake app id and then teaching the receiver's
-- admission check to make an exception for it.
CREATE TABLE IF NOT EXISTS group_outbox (
    id               BIGSERIAL    PRIMARY KEY,
    group_id         TEXT         NOT NULL,
    peer_entity_id   INTEGER      NOT NULL,
    op               TEXT         NOT NULL,
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
    -- Per (group, peer): one logical message fanned out to two members is two
    -- rows, and two groups never share a row. The bare UNIQUE (message_id) that
    -- made a fan-out write onto its own first row is the defect v153 records.
    UNIQUE (group_id, peer_entity_id, message_id)
);

COMMENT ON TABLE group_outbox IS
    'Every outbound group-lane send, written BEFORE the first attempt. op is '
    'the wire op (group.grant | group.vote). status: queued | delivered | '
    'refused | expired. A transport failure is retried with backoff until '
    'expires_at; a refusal is the far side''s authoritative answer and is '
    'never retried.';

CREATE INDEX IF NOT EXISTS group_outbox_due_idx
    ON group_outbox (next_attempt_at) WHERE status = 'queued';
