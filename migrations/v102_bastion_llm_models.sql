-- v102 (2026-08-03): OpenAI-compatible local model server on a bastion.
--
-- A bastion may serve models from TWO servers at once: ollama (advertised in `models`, reached on
-- 11434) and an OpenAI-compatible server — llama.cpp's llama-server, vLLM — advertised here. They
-- MUST stay separate: they answer on different ports, and mastercode.model_backends.list_local_models
-- resolves each list against its own port. Merging them into `models` forces one derived port onto
-- both, so the host dials ollama's port for a model only llama.cpp serves and the call fails 404 —
-- which is exactly the bug this fixes (a box serving Qwen 3.6 on llama-server).
--
-- llm_port is the bastion's TUNNEL-SIDE forward for that server (the gateway's serveLLMForward), not
-- the server's address on the user's LAN — the entity pod has no route to the LAN, only to the mesh.
-- NULL means "use the default port", never "no server"; emptiness is carried by llm_models = [].
--
-- The runtime ensures both columns defensively (topology.ensure_table ALTER … ADD COLUMN IF NOT
-- EXISTS) so a pod mid-migration never 500s on a heartbeat write; this is the authoritative record.
ALTER TABLE network_bastion_heartbeat ADD COLUMN IF NOT EXISTS llm_models JSONB NOT NULL DEFAULT '[]';
ALTER TABLE network_bastion_heartbeat ADD COLUMN IF NOT EXISTS llm_port INTEGER;
