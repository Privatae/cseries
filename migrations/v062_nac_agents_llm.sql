-- v062 (2026-06-20): NAC agent governance P9.1 — the network signal.
--
-- P9.0 records the PROCESS signal (known agent binaries). P9.1 adds the NETWORK signal: the bastion
-- flags outbound connections to LLM endpoints (correlated by pid), so a process disguised at the
-- process layer still betrays itself by talking to an LLM. The endpoints it's talking to are stored
-- per observation. The runtime ensures this column defensively (nac_agents.ensure_table ALTER … ADD
-- COLUMN IF NOT EXISTS) so a pod mid-migration never breaks; this is the authoritative record.
ALTER TABLE nac_agent_activity ADD COLUMN IF NOT EXISTS llm_endpoints JSONB;
