-- v137 — what a machine can turn into TEXT.
--
-- The bastion advertises a 'context-prep' capability plus a LIST of the jobs it
-- actually has a working tool for (contextprep.go). capabilities already has a
-- column; the list does not, and the heartbeat writer drops fields it has no
-- column for — so without this the host would see the capability and have no way
-- to ask "can this box turn a PDF into text", which is the only question it ever
-- asks.
--
-- prep_scan is the QUIET-VS-BLIND half, and it is why this is two columns rather
-- than one. An empty prep_tools from a box that probed three binaries and found
-- none is a different fact from an empty list on a build that cannot probe at
-- all, and the host must not read them the same way. Same rule the heartbeat
-- already applies to agents (agent_scan) and schedulers (scheduler_scan).
--
-- NULL, not '[]', on a bastion that predates the feature: absence has to stay
-- distinguishable from "looked and found nothing". A DEFAULT '[]' here would
-- make every old box in the fleet claim it had looked.

DO $$
BEGIN
    IF to_regclass('public.network_bastion_heartbeat') IS NULL THEN
        RAISE NOTICE 'v137: network_bastion_heartbeat absent — nothing to alter';
        RETURN;
    END IF;

    ALTER TABLE network_bastion_heartbeat
        ADD COLUMN IF NOT EXISTS prep_tools JSONB;
    ALTER TABLE network_bastion_heartbeat
        ADD COLUMN IF NOT EXISTS prep_scan  JSONB;
END $$;
