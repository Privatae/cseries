-- v146 — the detection plane's summary line reaches the host.
--
-- The bastion has computed this since 0.30.0: one pass per heartbeat over
-- everything running and every standing job, compared against the machine's
-- signed accounting, noise-filtered, levelled. It sends the summary on every
-- beat under the key `findings` (client-go/agents.go:648).
--
-- And the host had nowhere to put it. The heartbeat writer takes a fixed list of
-- columns and this was not one of them, so the plane computed a critical, sent
-- it, and topology.upsert_heartbeat dropped it on the floor — the summary
-- reached the host's door and no further. That is the whole defect this column
-- fixes; the detection half was already shipped and working.
--
-- WHY `detect` AND NOT `findings`. The key names what it is about, not what it
-- carries: the findings themselves stay on the box, where `observe:delta`
-- prints them, and they are meant to land in a machine store the host reads
-- through `dcn` (not built yet). A column called `findings`
-- holding counts is how a later reader ends up querying it for rows that were
-- never in it.
--
-- NULL, not an empty summary, on a bastion that predates the plane or one whose
-- first pass has not completed. Absence has to stay distinguishable from a
-- completed comparison, and here that matters more than usual: a DEFAULT of
-- zero counts would make every old box in the fleet read as a machine that was
-- checked and found clean. The state of the accounting travels INSIDE the
-- summary for the same reason — `baseline: none` means nobody ever said what
-- normal was on that machine, which is the opposite of an all-clear and is one
-- field away from looking identical to it.
--
-- Narrowed at the trust boundary before it is stored, like every other reported
-- field: pairing_grants.narrow_detect (whitelisted keys, capped strings, counts
-- clamped, and `baseline` required to be a state this pod knows).

DO $$
BEGIN
    IF to_regclass('public.network_bastion_heartbeat') IS NULL THEN
        RAISE NOTICE 'v146: network_bastion_heartbeat absent — nothing to alter';
        RETURN;
    END IF;

    ALTER TABLE network_bastion_heartbeat
        ADD COLUMN IF NOT EXISTS detect JSONB;

    -- INSIDE THE GUARD (v150 copied this file's shape and hit the bug on a
    -- real database). A bare COMMENT after the DO
    -- block is NOT covered by the to_regclass check above: on a pod without
    -- network_bastion_heartbeat the guard returned cleanly and the next
    -- statement raised `relation does not exist`, which entrypoint.sh:356 runs
    -- under ON_ERROR_STOP=1 — i.e. the host would not come up, in exactly the
    -- case this guard exists for. Safe to change after shipping: the migration
    -- loop reads schema_migrations and runs each version once in a host's life,
    -- so every live host already applied v146 and this only reaches a host
    -- built from empty.
    COMMENT ON COLUMN network_bastion_heartbeat.detect IS
        'Detection plane summary as REPORTED by the bastion: counts '
        'per level plus the state of the accounting they were computed against. '
        'NOT the findings — those stay on the box. NULL = this box never '
        'reported a pass, which is never the same as "no findings".';
END $$;
