-- v150 — the machine's ALERT summary reaches the host.
--
-- Same defect class as v146, one plane over. The bastion reads the alert source
-- on its own box (Alertmanager on that machine's own network, endpoint placed by
-- the admin at install — never by the host) and folds a summary onto every
-- heartbeat under the key `alerts`. topology.upsert_heartbeat takes a FIXED
-- column list, so anything not named in it is dropped on the floor: exactly how
-- the detection plane's summary reached the host's door and no further until
-- v146. This column is the place to put it.
--
-- WHAT IT CARRIES: counts, a window, and up to 20 fingerprints. Not the alerts.
-- The rows stay on the box, where `observe:alerts` pulls them on demand — the
-- same split v146 made for `findings`, and for the same reason. A column holding
-- counts under a name that sounds like rows is how a later reader ends up
-- querying it for rows that were never in it. `fingerprints` is the exception
-- that proves it: they are identifiers to ask the box about, never the alert
-- bodies, and they are capped so the beat cannot grow without bound.
--
-- NULL MEANS NEVER REPORTED, AND THAT IS NOT "NO ALERTS". This is the whole
-- reason the column is nullable with no DEFAULT. A bastion with no alert source
-- configured omits the key entirely, and so does one that predates the op
-- family. A DEFAULT of zero counts would make every such machine — which today
-- is every machine in the fleet — read as a box that was checked and found
-- quiet. "Nobody is watching this machine's alerts" and "this machine has no
-- firing alerts" are the two answers an operator most needs kept apart, and they
-- are one field away from looking identical.
--
-- Narrowed at the trust boundary before it is stored, like every other reported
-- field: pairing_grants.narrow_alerts (counts must be non-negative integers,
-- fingerprints a list of strings capped at 20, and the two timestamps either
-- parseable or absent). Anything malformed returns None and is stored as NULL —
-- it reads as "never reported", never as "quiet", because a payload this pod
-- cannot read is a box saying something unintelligible and not a box reporting
-- calm.

-- THE COMMENT RUNS INSIDE THE GUARD, and that is not tidiness. entrypoint.sh:356
-- applies every migration with ON_ERROR_STOP=1 and a failure ABORTS STARTUP. A
-- bare `COMMENT ON COLUMN` after the DO block is not covered by the to_regclass
-- check above it, so on a pod where network_bastion_heartbeat does not exist the
-- guard would print its notice, return cleanly, and the very next statement would
-- raise `relation does not exist` and stop the host coming up — the one outcome
-- the guard was written to prevent. Measured against a real
-- cseries-postgres:smoke: a draft of this file did exactly that.
-- v146_bastion_detect_summary.sql carries the same shape and is fixed the
-- same way; it is already recorded in schema_migrations on every
-- live host, so the fix only ever reaches a host built from empty.
DO $$
BEGIN
    IF to_regclass('public.network_bastion_heartbeat') IS NULL THEN
        RAISE NOTICE 'v150: network_bastion_heartbeat absent — nothing to alter';
        RETURN;
    END IF;

    ALTER TABLE network_bastion_heartbeat
        ADD COLUMN IF NOT EXISTS alerts JSONB;

    COMMENT ON COLUMN network_bastion_heartbeat.alerts IS
        'Alert summary as REPORTED by the bastion from the alert source on its '
        'own box: firing/critical/warning counts, the '
        'window they cover, and up to 20 fingerprints to ask the box about. NOT '
        'the alerts themselves — those stay on the machine and `observe:alerts` '
        'pulls them. NULL = this box never reported a summary, i.e. it has no '
        'alert source configured or its bastion predates the feature. That is '
        'never the same as "no alerts firing".';
END $$;
