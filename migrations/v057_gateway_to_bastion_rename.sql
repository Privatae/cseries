-- v057 (2026-06-18): Rename the secure-door concept gateway -> bastion.
--
-- The user's secure-door machine — the host's "bastion" — was keyed in code +
-- DB by the legacy word "gateway": mcp_client_tokens.operator_kind='gateway',
-- the network_gateway_heartbeat table (v056), and its ix_gateway_hb_last_seen
-- index. The rename to 'bastion' is now done across the repo (topology.py,
-- routes/network.py, gateway_enroll.py, the Network app); this migration brings
-- already-provisioned entity DBs in line so list/revoke (which now query
-- operator_kind='bastion' against network_bastion_heartbeat) keep working.
--
-- Idempotent: IF EXISTS on the renames; the operator_kind UPDATE is naturally
-- a no-op on re-run (no rows still hold the old value). The TEXT column type and
-- the generic operator_kind plumbing are untouched — only the literal VALUE
-- 'gateway' migrates to 'bastion'.

ALTER TABLE IF EXISTS network_gateway_heartbeat RENAME TO network_bastion_heartbeat;
ALTER INDEX IF EXISTS ix_gateway_hb_last_seen RENAME TO ix_bastion_hb_last_seen;
UPDATE mcp_client_tokens SET operator_kind = 'bastion' WHERE operator_kind = 'gateway';
