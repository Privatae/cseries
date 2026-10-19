-- v118 (2026-08-29): managed FreeAgent state on a bastion.
--
-- The bastion (gateway 0.13.2+) installs and runs the user's FreeAgent
-- for every machine store whose grant carries 'search', and reports two
-- things in every heartbeat: the `freeagent` capability (present only when
-- an instance is SERVING) and `freeagent_stores` — per store id:
--   {"state": installing|running|missing-python|missing-openssl|failed,
--    "serving": bool, "version": "<artifact version>", "detail": "<why>"}.
-- Until now only the capability was stored, so the one thing a member has to
-- fix (Python, openssl) was invisible without asking the host to probe. This
-- keeps the detail so Context Manager's store card and the Network app's
-- FreeAgents lens can say it.
--
-- freeagent_ok mirrors pii_ok: the bastion's own verdict (all instances
-- serving); NULL = not advertised or pre-0.13 bastion.
-- The runtime ensures both columns defensively (topology.ensure_table ALTER …
-- ADD COLUMN IF NOT EXISTS); this is the authoritative record.
ALTER TABLE network_bastion_heartbeat ADD COLUMN IF NOT EXISTS freeagent_ok BOOLEAN;
ALTER TABLE network_bastion_heartbeat ADD COLUMN IF NOT EXISTS freeagent_stores JSONB NOT NULL DEFAULT '{}';
