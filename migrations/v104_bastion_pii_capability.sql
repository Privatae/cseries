-- v104: PII sterilization service advertisement on a bastion.
--
-- The bastion-side forwarder (the gateway's servePIIForward, tunnel-side :18081) proxies to
-- wherever the PII service actually runs on the user's LAN — the platform never learns or cares.
-- pii_port is the TUNNEL-SIDE port; mastercode.pii_client.resolve_endpoint dials
-- http://<reported_ip>:<pii_port> and treats pii_port IS NULL as NOT ADVERTISED — the honest
-- disabled state (the fail-closed write path gates on it). Unlike llm_port, absence is
-- meaningful here, so the column is NULL when the bastion does not advertise the lane at all.
--
-- pii_ok is the bastion's last /health probe of its upstream: an up forwarder with a dead
-- service is a distinct state (advertised-but-unhealthy) the write path can gate on instead of
-- discovering the failure one 502 at a time. NULL = not advertised or pre-upgrade bastion.
--
-- The runtime ensures both columns defensively (topology.ensure_table ALTER … ADD COLUMN IF NOT
-- EXISTS) so a pod mid-migration never 500s on a heartbeat write; this is the authoritative record.
ALTER TABLE network_bastion_heartbeat ADD COLUMN IF NOT EXISTS pii_port INTEGER;
ALTER TABLE network_bastion_heartbeat ADD COLUMN IF NOT EXISTS pii_ok BOOLEAN;
