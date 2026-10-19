-- v163 — holes in a machine's own record reach the host.
--
-- The bastion keeps its findings, alerts and observe results in a store on its
-- own disk (client-go/machinestore.go). A result it could NOT write — a full
-- disk, a sealed day-file — is counted per shelf and sent on every beat under
-- the key `machine_record_dropped`, so a record with holes is never mistaken
-- for a quiet machine.
--
-- And the host had nowhere to put it. The heartbeat writer takes a fixed
-- column list and this was not on it, so the count reached the host's door and
-- no further — the same defect v146 fixed for the detection counts and v150
-- for the alert counts, one beat field over: the bastion's observe shelf had
-- no reader anywhere in cseries.
--
-- THREE VALUES, and they must stay three: an EMPTY OBJECT when the box says
-- its record is whole; a populated object for holes, per shelf; NULL when the
-- box sent nothing — a bastion that predates the shelf, or a pairing not
-- entitled to observe. NULL is "not reported", exactly as it is for detect
-- and alerts, and the update path COALESCEs like theirs. (A first version
-- collapsed whole into NULL and overwrote; the observe switch in the bastion
-- console then made a populated column read as whole while the holes
-- persisted. The box sends {} when whole.)
-- The counter covers the observe shelves only.
--
-- Narrowed at the trust boundary before it is stored, like every reported
-- field: pairing_grants.narrow_record_dropped (shelf names charset-checked,
-- counts clamped, at most 32 shelves).

DO $$
BEGIN
    IF to_regclass('public.network_bastion_heartbeat') IS NULL THEN
        RAISE NOTICE 'v163: network_bastion_heartbeat absent — nothing to alter';
        RETURN;
    END IF;

    ALTER TABLE network_bastion_heartbeat
        ADD COLUMN IF NOT EXISTS record_dropped JSONB;

    -- Inside the guard, for the reason v146 records: a bare COMMENT after the
    -- DO block is not covered by the to_regclass check and aborts the migration
    -- on a pod without the table.
    COMMENT ON COLUMN network_bastion_heartbeat.record_dropped IS
        'Per-shelf count of results the bastion could NOT write into its own '
        'machine record, as REPORTED on the beat. {} = the box '
        'says its record is whole; NULL = not reported (a bastion predating '
        'the shelf, or a pairing not entitled to observe). Observe shelves only.';
END $$;
