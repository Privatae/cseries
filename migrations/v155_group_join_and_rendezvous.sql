-- v155: the two ways a host reaches a group it is not in yet —
-- a JOIN REQUEST it sends itself, and a RENDEZVOUS introduction the platform
-- batches for it. Join requests are the hook the store audience needs.
--
-- DDL lives here and nowhere else: CREATE TABLE IF NOT EXISTS /
-- CREATE INDEX IF NOT EXISTS only, so this is a no-op on a host that already
-- has the tables and the full shape on an empty one.
--
-- NO BACKFILL. Nothing here reads or rewrites an existing row.

BEGIN;

-- ── The knock vocabulary. ──────────────────────────────────────────────────
-- inbox_messages.message_kind carries a CHECK constraint, last set by
-- v026_decisions.sql:112-121 and mirrored in init-db.sql:1938-1946. Two kinds
-- the peer lane already SENDS are missing from it:
--
--   machine_request   handlers.py declares it in
--                     REQUEST_KINDS and passes it verbatim into the payload
--                     memory_handlers.py:463 inserts, so its card cannot be
--                     stored: the INSERT raises, handle_inbox_append catches it
--                     and returns {"ok": False}, and the asking host is told its
--                     knock was refused. MEASURED, not inferred — a Postgres
--                     18.4 with exactly the v026 constraint refuses
--                     INSERT ... VALUES ('machine_request') and admits
--                     ('endpoint_request') in the same session. Fixed here
--                     rather than filed, because this migration has to open the
--                     vocabulary anyway and leaving a shipped lane broken next
--                     to the line that would have fixed it is not a smaller
--                     change, it is the same change with a defect left in.
--
--   group_join        new here: one host asking another to be let into a group
--                     circle whose roster that host owns.
--
--   rendezvous_intro  new here: the platform's batched INTRODUCTION into a
--                     community group. It is a card, not a grant — it names a
--                     group and a count and confers nothing, and the member's
--                     Host joins only when the user says yes.
--
-- The constraint is re-stated in full rather than extended, because Postgres
-- has no ADD VALUE for a CHECK: DROP and re-ADD is the only shape, and it is
-- the shape v026 itself used.
ALTER TABLE inbox_messages
    DROP CONSTRAINT IF EXISTS inbox_messages_message_kind_check;

ALTER TABLE inbox_messages
    ADD CONSTRAINT inbox_messages_message_kind_check
        CHECK (message_kind IN (
            'message','permission_grant','permission_request',
            'endpoint_request','endpoint_grant','endpoint_revoke',
            'software_order','software_status',
            'contract_proposal','contract_accept','contract_reject',
            'system_notification',
            'decision','decision_review',
            -- v155. See the block above for each.
            'machine_request','group_join','rendezvous_intro'
        ));

-- ── What this host has ASKED to join. ──────────────────────────────────────
-- The receiving side of a join request needs no table: the knock lands as an
-- ordinary inbox_messages card (message_kind='group_join', payload->>'group_id')
-- and the owner's accept/deny reads it there, exactly as endpoint_request does.
-- This table is the SENDING side, and it exists for one reason the card cannot
-- serve: the asker's own card is on the OTHER host. Without a local row the
-- asking user has no way to see that they asked, and the UI would offer "ask to
-- join" for ever against a group whose owner has simply not answered yet.
--
-- Keyed by group alone: one group, one owner, one outstanding ask. A second ask
-- for the same group is the same question again, so it updates this row rather
-- than making a second one — the shape _knock_slot_free enforces on the
-- receiving side (server/peer/handlers.py:812-842).
CREATE TABLE IF NOT EXISTS group_join_requests (
    group_id         TEXT         PRIMARY KEY,
    owner_entity_id  INTEGER      NOT NULL,
    -- What the far side said the last time we asked. 'sent' the knock landed,
    -- 'refused' the owner's host answered no (a full slot, a closed group, a
    -- group it does not own), 'unreachable' the transport failed. NOT an
    -- answer to the QUESTION: an accept arrives as a group.grant and a denial
    -- arrives as nothing at all, which is the whole point of a silent deny.
    status           TEXT         NOT NULL DEFAULT 'sent',
    last_error       TEXT,
    note             TEXT         NOT NULL DEFAULT '',
    asked_at         TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ  NOT NULL DEFAULT now()
);

COMMENT ON TABLE group_join_requests IS
    'A join request THIS host sent to a group''s owner. The owner''s copy is an '
    'inbox_messages card; this is the asker''s own record so the user can see '
    'that they asked. An accept arrives as a group.grant on the peer lane; a '
    'denial arrives as nothing, so this row never turns into "denied".';

-- ── Which of our published apps are CIRCLE apps, and whose roster gates them.
-- The audience `circle`, and the hook: "the membership source that the
-- catalog filter and the bundle-route gate read is `group_members` on the
-- creator's host, keyed by the group id the store app is published to."
--
-- THE STORE IS PLATFORM-SIDE AND THE ROSTER IS HOST-SIDE. That is the whole
-- problem this table exists to solve. The platform cannot read a creator-held
-- roster — it holds nothing for a group circle, by design (v154's header) — so
-- the creator's host has to TELL it, and something has to remember which apps
-- that obligation applies to. That is this table.
--
-- The mechanism is a PUSH ON CHANGE, not a platform pull: a pull would need the
-- platform to hold a credential against every publisher's host and to know when
-- to use it, which is a standing reach into the pod for a fact the pod can
-- simply send. The push rides the group lane's existing beat — the reconciler
-- already wakes on every roster resync (server/groups/lane.py:743-757), which
-- is exactly the moment the roster this host owns is known to be current.
--
-- posted_epoch is what the PLATFORM last acknowledged, not what we last sent.
-- Storing the send would make a failed post look applied on the next pass and
-- the removal it carried would never arrive — and a removal is the change that
-- matters, because it is the one that cuts off a former member's access to the
-- bundle.
CREATE TABLE IF NOT EXISTS app_store_circle_publications (
    app_id        TEXT         PRIMARY KEY,
    group_id      TEXT         NOT NULL,
    posted_epoch  BIGINT,
    posted_at     TIMESTAMPTZ,
    last_error    TEXT,
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ  NOT NULL DEFAULT now()
);

COMMENT ON TABLE app_store_circle_publications IS
    'An app this host published to the store with audience=circle, and the '
    'group whose roster gates it. posted_epoch is the roster_epoch the PLATFORM '
    'acknowledged — never the one we last sent — so a failed post is retried '
    'rather than silently treated as applied.';

CREATE INDEX IF NOT EXISTS app_store_circle_publications_group_idx
    ON app_store_circle_publications (group_id);

-- ── Rendezvous: the tags this host has listed. ─────────────────────────────
--
-- ONLY THE TAG LIST LEAVES THE POD. Not memory, not profile, not a summary of
-- either. This table is the whole of what the platform is ever told, which is
-- why it is a table of bare strings and carries no free-text description field
-- — a field nobody can see the far side of is a field that will eventually be
-- filled with something that should not have left.
--
-- Normalised at write: lowercase, trimmed, inner whitespace collapsed to one
-- '-'. Stored normalised rather than normalised on read, because the platform
-- matches on exactly these bytes and a tag that reads differently here than it
-- matched there is the bug that is impossible to see.
--
-- NO CAP ON COUNT. A person's interests are theirs; capping them caps the
-- point (the same rule the roster is uncapped under). What IS
-- bounded is the length of one tag, because it is an untrusted-ish string that
-- becomes a match key and a group name on the platform side.
CREATE TABLE IF NOT EXISTS rendezvous_interests (
    tag           TEXT         PRIMARY KEY,
    -- The platform's answer to our last post of this tag. Kept so a withdraw
    -- knows whether there is anything to withdraw, and so the UI can say
    -- "listed" rather than "we tried".
    listed_at     TIMESTAMPTZ,
    last_error    TEXT,
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    withdrawn_at  TIMESTAMPTZ
);

COMMENT ON TABLE rendezvous_interests IS
    'Free-text interest tags this host has offered to the rendezvous batch. '
    'Normalised lowercase at write; the stored bytes ARE the bytes the platform '
    'matches on. This table is the complete set of what rendezvous ever sends '
    'off the pod — never memory, never profile.';

CREATE INDEX IF NOT EXISTS rendezvous_interests_live_idx
    ON rendezvous_interests (tag) WHERE withdrawn_at IS NULL;

-- ── The listing counter. ───────────────────────────────────────────────────
-- ONE ROW, ENFORCED BY THE PRIMARY KEY AND A CHECK. `seq` is monotonic per
-- host and is the anti-replay on the signed listing: the platform refuses a
-- listing at or below the seq it already holds, so an old signed list cannot be
-- replayed to resurrect a tag the user withdrew. That is `held_seq`'s rule
-- (server/groups/roster.py:477-492) applied to the same problem.
--
-- It is a table and not a counter derived from rendezvous_interests because the
-- seq belongs to the POST, not to a tag: two posts of the same tag set are two
-- seqs, and the second must supersede the first. Deriving it from the tag rows
-- would make "I re-listed the same interests" unsignable.
--
-- A separate row per key id is not needed and would be wrong: there is one
-- entity_sign key per pod, first-key-wins at the platform, and a host that
-- somehow presented a second key is an alarm there rather than a second
-- counter here (server/groups/roster.py:266-298).
CREATE TABLE IF NOT EXISTS rendezvous_state (
    id          INTEGER      PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    seq         BIGINT       NOT NULL DEFAULT 0,
    updated_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
);

COMMENT ON TABLE rendezvous_state IS
    'One row. seq is this host''s monotonic rendezvous listing counter — the '
    'anti-replay on the signed tag list. Bumped on every post, never reset: a '
    'reset would make every previously-signed listing replayable.';

COMMIT;
