-- v156 — the host's oversight ledger, and the third summary the beat was
-- already carrying.
--
-- Numbered v156: v153 (peer app lane), v154 (groups roster votes) and
-- v155 (group join + rendezvous) are taken. The two events this ledger writes, oversight_critical and
-- oversight_high, are the ones v152's own header declared and could not emit
-- yet; this is what emits them.
--
-- TWO THINGS, and they are one change because neither is useful alone.
--
-- 1. `oversight_findings` — the ledger. Everything the host senses about the
--    machines it manages folds into ONE row per thing-that-is-wrong, with a
--    moving `last_seen`. The alternative — appending an event per pass — is
--    what a 5-minute standing op turns into a pile: the same fail-open firewall
--    would be 288 rows a day, and the operator question ("what is wrong, and
--    since when") becomes a GROUP BY over a log instead of a SELECT. `key` is
--    UNIQUE and carries (machine, kind, fingerprint), so the second pass over
--    the same fact updates rather than accumulates.
--
--    `first_seen` IS THE VALUE. It is the one field nothing else on this host
--    records: a heartbeat says what is true now, and every summary it carries
--    is a present-tense snapshot that the next beat overwrites. "This machine's
--    pubwall has been fail-open since Tuesday" cannot be derived from any of
--    them, and it is the sentence an operator acts on. So `first_seen` is set
--    once at insert and never moved, and `last_seen` moves on every pass.
--
--    `resolved_at` IS NULLABLE AND MOSTLY STAYS NULL, on purpose. A finding is
--    NOT auto-closed because it stopped being reported: the overwhelmingly
--    common reason a machine stops reporting a problem is that the machine
--    stopped reporting, and closing the ledger row on a box that went offline
--    would turn "we lost sight of this" into "this was fixed" — the same
--    quiet-versus-blind failure the detect and alert planes each have a
--    vocabulary to prevent, arriving here through the back door. The one
--    auto-resolve is narrow and stated in the code: an alert fingerprint that is
--    absent from the CURRENT summary of an ONLINE machine whose alert source is
--    answering. That is a box that looked and no longer sees it.
--
--    `evidence` is JSONB and it is the summary the finding was folded from, not
--    the machine's rows. The rows stay on the box (`observe:alerts`,
--    `observe:delta`) — the same split v146 and v150 made, for the same reason.
--
-- 2. `network_bastion_heartbeat.posture` — the third dropped summary.
--    the gateway's `buildHeartbeatBody` has folded `posture` onto every beat
--    (client-go/agents.go:651-657, the compact HOST_PROBE subset at
--    client-go/observe_ops.go:126-139), and this pod's heartbeat writer takes a
--    FIXED column list, so it has been dropped on the floor exactly the way
--    `detect` was before v146 and `alerts` was before v150. Three planes, one
--    defect, three migrations. The ledger's posture rules read this column;
--    without it that half of the standing op has nothing to fold.
--
--    NULL MEANS THE BOX NEVER REPORTED ONE — an older bastion, a non-Linux box
--    (there is no posture key off Linux, observe_ops.go:810), or the first beat
--    after start. It is never "this machine's posture is fine". The ledger only
--    ever writes FINDINGS, never all-clears, which is what keeps that
--    distinction structural here rather than a matter of wording: a field the
--    box did not report produces no row, and a row is the only thing anybody
--    reads.
--
-- THE COMMENTS RUN INSIDE THE GUARD. entrypoint.sh:356 applies every migration
-- with ON_ERROR_STOP=1 and a failure ABORTS STARTUP; a bare `COMMENT ON COLUMN`
-- after a DO block is not covered by the to_regclass check above it. v150's
-- header records the day that cost a boot on a real cseries-postgres:smoke.

CREATE TABLE IF NOT EXISTS oversight_findings (
    id              BIGSERIAL PRIMARY KEY,
    -- (machine, kind, fingerprint), joined. UNIQUE is the pin: one row per
    -- thing, with a moving last_seen, never a pile.
    key             TEXT NOT NULL UNIQUE,
    severity        TEXT NOT NULL
                        CHECK (severity IN ('notice', 'warn', 'high', 'critical')),
    -- Which plane produced it. `correlation` is unused by the deterministic
    -- ingest and is reserved for the scheduled model turn, which is a
    -- separate decision after a measured day.
    source          TEXT NOT NULL
                        CHECK (source IN ('heartbeat', 'alert', 'observe', 'correlation')),
    machine         TEXT,
    first_seen      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen       TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at     TIMESTAMPTZ,
    evidence        JSONB,
    recommendation  TEXT,
    pr_url          TEXT
);

-- The ledger's one hot query: open findings, worst first. `resolved_at` leads
-- because every read the app and the standing op make is scoped to open rows
-- (resolved_at IS NULL) and severity only orders within that.
CREATE INDEX IF NOT EXISTS oversight_findings_open_severity_idx
    ON oversight_findings (resolved_at, severity);

DO $$
BEGIN
    IF to_regclass('public.oversight_findings') IS NOT NULL THEN
        COMMENT ON TABLE oversight_findings IS
            'The host oversight ledger: ONE row per '
            '(machine, kind, fingerprint) with a moving last_seen and a '
            'first_seen that never moves. Folded deterministically from what '
            'the machines already report on their heartbeats — no model '
            'anywhere in this path. A row is never auto-closed because it '
            'stopped being reported; see resolved_at.';
        COMMENT ON COLUMN oversight_findings.first_seen IS
            'When this host FIRST saw this fact. Set once, never moved. It is '
            'the one thing no heartbeat can tell you: every summary a beat '
            'carries is present-tense and the next beat overwrites it.';
        COMMENT ON COLUMN oversight_findings.resolved_at IS
            'NULL for an open finding, and it stays NULL when the fact simply '
            'stops being reported — a machine that went offline has not been '
            'fixed. The only automatic close is an alert fingerprint absent '
            'from the current summary of an ONLINE machine whose alert source '
            'is answering.';
        COMMENT ON COLUMN oversight_findings.evidence IS
            'The heartbeat summary this finding was folded from — counts, '
            'states and fingerprints. NOT the machine''s rows: those stay on '
            'the box, where observe:alerts / observe:delta list them.';
    END IF;

    IF to_regclass('public.network_bastion_heartbeat') IS NULL THEN
        RAISE NOTICE 'v153: network_bastion_heartbeat absent — posture column skipped';
        RETURN;
    END IF;

    ALTER TABLE network_bastion_heartbeat
        ADD COLUMN IF NOT EXISTS posture JSONB;

    COMMENT ON COLUMN network_bastion_heartbeat.posture IS
        'Host posture as REPORTED by the bastion: the compact '
        'HOST_PROBE subset it refreshes every 5 minutes on its own box — ufw, '
        'pubwall, ssh22, f2b, sec, load, cpus, reboot, diskpct, jrn '
        '(the gateway client''s observe_ops.go). `input` and `puv` '
        'are NOT on the beat and stay on observe:host. NULL = this box never '
        'reported one: a bastion too old to send it, a non-Linux box (there is '
        'no posture key off Linux), or the first beat after start. That is '
        'never the same as "this machine''s posture is fine".';
END $$;
