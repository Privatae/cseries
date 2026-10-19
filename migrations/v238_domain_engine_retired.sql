-- v238 — the domain controller's decision engine is retired.
--
-- On a correctly granted Host the controller's grant checks refused nothing
-- the capability gate does not already refuse (scripts/test_ceigas_gate_routes.py
-- asserted zero refusals on the live grants). The one refusal unique to it
-- that is wanted, the operator kill switch, is a file check every surface now
-- makes directly (brain/ceigas_kill_switch.py). Trust quarantine goes with the
-- engine: the score moved only with the engine's own crossings and the
-- platform's trust_ledger.upsert, and a stop from outside the Host is not a
-- path the platform takes; the kill switch is the stop.
--
-- Dropped, with the code that wrote and read them:
--   domain_synapse        the synapse / rate-limit / modulation table
--   domain_signal_log     the per-crossing audit
--   domain_signal_rollup  its daily counts; no reader
--   trust_ledger          the trust score; its quarantine read went with it
-- Kept: entity_domain_access (the capability gate's store), domain_registry
-- (its foreign-key parent) and vault_audit_rollup (the credential audit's
-- retention bound, now server/vault/audit_rollup.py).
--
-- On a fresh database v019 re-creates domain_synapse, domain_signal_log and
-- trust_ledger (CREATE IF NOT EXISTS) for v036 and v038 to insert into, and
-- v196 / v205 create the two rollups; this migration drops them at the end
-- of the chain. Forward-only; idempotent.

BEGIN;

DROP TABLE IF EXISTS domain_synapse CASCADE;
DROP TABLE IF EXISTS domain_signal_log CASCADE;
DROP TABLE IF EXISTS domain_signal_rollup CASCADE;
DROP TABLE IF EXISTS trust_ledger CASCADE;

COMMIT;
