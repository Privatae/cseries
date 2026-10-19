-- v136: second-bastion detection — the anchor is told when another bastion is
-- installed on a machine it anchors
--
-- The anchor host should be alerted when a new bastion is installed on the
-- machine.
--
-- This is the second half of the machine singleton. The first half
-- (the machine marker, the lock, and the pair refusal) REFUSES a
-- second bastion going forward. It cannot do anything about two cases:
--
--   1. Installs that predate the marker. Every pairing already on disk stays
--      there; refusing new ones does not remove old ones. On the estate today
--      this is the only case that exists.
--   2. Anything that got around the refusal — a build with the guard removed,
--      a state directory restored from backup, a marker deleted and rewritten.
--
-- So the daemon LOOKS: it reads the machine's users and reports every bastion
-- state directory or binary that is not its own (client-go/otherbastions.go).
--
-- NULL vs '[]' IS LOAD-BEARING HERE IN A WAY IT IS NOT FOR peers, and this is
-- the whole reason there is no DEFAULT:
--   NULL = the box DID NOT LOOK. It is not root, not Linux, or too old to have
--          the scan. This is "unknown", and no reader may render it as a clean
--          machine — a detection feature that reports "nothing found" off a box
--          that never checked is worse than having no detection at all.
--   '[]' = it looked and the machine is clean. A real state change, so it
--          travels and clears a finding that has been dealt with.
--
-- Narrowed at the boundary in pairing_grants.narrow_other_bastions before it is
-- ever written here: fixed `kind` set, capped list, whitelisted fields. The
-- paths name files in ANOTHER OS user's home directory and are only ever
-- displayed — nothing downstream opens one.
--
-- Idempotent; mirrors the defensive ADD COLUMN in topology.ensure_table.

BEGIN;

ALTER TABLE network_bastion_heartbeat
    ADD COLUMN IF NOT EXISTS other_bastions JSONB;

COMMENT ON COLUMN network_bastion_heartbeat.other_bastions IS
    'Other bastion installations this box found on its own machine, as it '
    'reports them: [{kind, user, uid, state_dir, binary_path, pairings, '
    'last_change}]. kind is paired|state|binary. NULL = the box did not look '
    '(not root, not Linux, or pre-0.21.0) and means UNKNOWN, never clean; '
    '[] = it looked and found none.';

-- WHEN that scan completed, as the box reported it (RFC3339).
--
-- The bastion publishes its LAST completed scan and deliberately does not
-- retract it on a failed one — a transient read error must not wipe a real
-- finding. The cost of that choice is that a scan which stops completing freezes
-- its answer for ever, and if the frozen answer is '[]' every reader shows a
-- clean machine indefinitely. A frozen all-clear is precisely the failure this
-- feature exists to prevent, so the freshness travels beside the finding and the
-- readers age it out instead of trusting it.
ALTER TABLE network_bastion_heartbeat
    ADD COLUMN IF NOT EXISTS other_bastions_scanned_at TEXT;

COMMENT ON COLUMN network_bastion_heartbeat.other_bastions_scanned_at IS
    'RFC3339 time the box completed the scan that produced other_bastions. '
    'NULL when it has never completed one. A reader must age this out: a stale '
    '[] is UNKNOWN, not clean.';

-- The durable side, for the same reason network_bastion_peer has one: the
-- user-facing alert must be deduped across beats, restarts, and two pods racing
-- one heartbeat, and an in-memory diff of the JSONB column above cannot do it —
-- the statement that would supply the "before" also overwrites it.
--
-- Keyed on the OS USER, not the path. One user is one installation, and a path
-- that changes underneath it (a binary that grows a state directory beside it)
-- is the same installation getting further along — which is news worth raising
-- again, not a second finding. notified_kind is what makes that possible: it
-- records how far along the installation was when we last said something, so a
-- climb up binary → state → paired re-raises and a flat beat stays quiet.
--
-- A finding is never deleted. When the box stops reporting it, gone_at is set:
-- the anchor asked someone to remove a second bastion, and the record of it
-- having been there is the evidence that they did.
CREATE TABLE IF NOT EXISTS network_bastion_other (
    token_id       INTEGER NOT NULL,
    uid            INTEGER NOT NULL,
    user_name      TEXT,
    kind           TEXT,
    state_dir      TEXT,
    binary_path    TEXT,
    pairings       INTEGER NOT NULL DEFAULT 0,
    first_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- How far along it was when the user was last told. NULL = never told.
    notified_kind  TEXT,
    -- Set only on a CONFIRMED inbox write. handle_inbox_append returns
    -- {"ok": false} rather than raising, so setting this on a bare await would
    -- record a failed notification as delivered and the dedupe would then
    -- guarantee the user was never told at all.
    notified_at    TIMESTAMPTZ,
    -- Last time the standing reminder fired (anchor only, and only while the
    -- second bastion is actually serving a host).
    raised_at      TIMESTAMPTZ,
    gone_at        TIMESTAMPTZ,
    PRIMARY KEY (token_id, uid)
);

COMMIT;
