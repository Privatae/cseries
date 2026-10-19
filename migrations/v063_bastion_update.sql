-- v063 (2026-06-20): bastion self-update path (phase 2) — the host's queued update target.
--
-- The host directs a bastion to self-update by setting update_to_version; the heartbeat response
-- then carries {"update":{"version":X}} until the bastion reports that version (the bastion fetches
-- the published manifest, verifies the sha256, swaps its own binary, and restarts). Host-directed +
-- hash-verified; the platform never serves the binary from here, only names the version. The runtime
-- ensures this column defensively (topology.ensure_table ALTER … ADD COLUMN IF NOT EXISTS); this is
-- the authoritative record.
ALTER TABLE network_bastion_heartbeat ADD COLUMN IF NOT EXISTS update_to_version TEXT;
