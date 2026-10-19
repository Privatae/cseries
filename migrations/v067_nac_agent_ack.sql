-- v067 (2026-06-20): NAC agent governance P9.4 — acknowledge.
--
-- A third disposition for unrequested activity, between sanction and contain: ACKNOWLEDGE. The user
-- has seen the agent and accepts it for now WITHOUT issuing a delegation tag — it leaves the
-- unrequested alert count (header chip + rail badge drop) and stops re-alerting, but it stays
-- UNREQUESTED (acknowledging is not sanctioning; it's "noted, not now"). Distinct from notified_at
-- (the system pushed an alert) — acknowledged_at is the USER's dismissal. Revoking is implicit: a new
-- pid is a new row, so a fresh agent re-alerts even on an acknowledged host. The runtime ensures this
-- column defensively (nac_agents.ensure_table); authoritative record here.
ALTER TABLE nac_agent_activity ADD COLUMN IF NOT EXISTS acknowledged_at TIMESTAMPTZ;
