-- v129: agent detection phase 1 — ORIGIN, EVIDENCE, AUTOMATION,
-- and the SCHEDULER INVENTORY (bastion 0.13.12).
--
-- Until now an observation said WHAT a process was (signature, kind) and
-- what it talked to. It could not say HOW IT WAS STARTED, which is the
-- whole question behind "an agent ran for months and nobody knew": model
-- traffic, a scheduler ancestor, no terminal. From bastion 0.13.12 every
-- observation carries:
--   origin    interactive | remote | scheduler | service | agent | unknown
--   evidence  [{signal, tier, value}] — every typed reason behind kind and
--             origin, tier verified / known / ambiguous; the Network app
--             shows the list, never a bare verdict (no model in the loop).
-- and kind gains a fourth value, `automation`: a browser driver, an input
-- injector, an RPA runtime — no model of its own. Inventory, never alerted,
-- promoted to `agent` by the bastion when model traffic joins it.
--
-- The heartbeat also carries the SCHEDULER INVENTORY: the crontab / systemd
-- timer / launchd / Task Scheduler entries whose command would run an agent
-- or automation, plus a scan report (which kinds were read, how many entries)
-- so an empty list from a box that could read nothing is not a quiet box.
--
-- NULL / [] on rows from older bastions. The runtime ensures every column
-- defensively (nac_agents.ensure_table, topology.ensure_table ALTER … ADD
-- COLUMN IF NOT EXISTS); this file is the authoritative record.
ALTER TABLE nac_agent_activity ADD COLUMN IF NOT EXISTS origin TEXT;
ALTER TABLE nac_agent_activity ADD COLUMN IF NOT EXISTS evidence JSONB;
ALTER TABLE network_bastion_heartbeat ADD COLUMN IF NOT EXISTS schedulers JSONB NOT NULL DEFAULT '[]';
ALTER TABLE network_bastion_heartbeat ADD COLUMN IF NOT EXISTS scheduler_scan JSONB;
