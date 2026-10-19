-- v179: the person's approval rides beside a directive
-- (bastion 0.33.0; server/network/nac_approval.py).
--
-- A bastion anchored to this Host no longer changes its machine on this Host's
-- word alone: every write — a management op, a shell, a self-update, a kill, a
-- mirror placement — runs on an approval the person gave on their own device
-- (or at that machine's console) and the box verified. A management op carries
-- its approval in the job frame; the three directives that ride the heartbeat
-- carry theirs beside the directive on the beat, and these columns are where
-- the Host keeps an approval it obtained until the beat delivers it.
--
-- NULL = not yet approved. The beat delivers a directive whose approval is
-- NULL only to a bastion that does not gate writes (one older than 0.33.0, or
-- an unanchored one); for a gating box the beat leaves it out and the
-- completion task (nac_approval.complete_directive_approvals) asks the device.

ALTER TABLE network_bastion_heartbeat ADD COLUMN IF NOT EXISTS update_approval TEXT;
ALTER TABLE nac_agent_contain         ADD COLUMN IF NOT EXISTS approval        TEXT;
ALTER TABLE nac_finding_contain       ADD COLUMN IF NOT EXISTS approval        TEXT;
