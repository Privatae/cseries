-- v237 — the tables nothing writes or reads.
--
-- Each table below was declared in init-db.sql and had no writer and no
-- reader in brain/, server/, sidecars/ or system-apps/ (checked with git
-- grep over the tree). Most never held a row on the oldest Host. The three
-- that had readers without a writer (dispatch_log, entity_functions,
-- entity_skills) lose those routes with this migration. entity_journal's
-- one writer (the synaptive shape audit) and entity_behavioral_log's writers
-- (the meta-tool gate, the synaptive gate, the slider audit, the memory-edit
-- audit, the bridge signal) go with the tables: a tool call the Host makes is
-- on tool_call_log, written by the runtime that made it, and that row now
-- carries the project the call named so the project's activity feed reads
-- it. codebook_recommendations and catalog_* were dropped by v094 and v206
-- already and are listed for the Hosts that predate those. Forward-only;
-- idempotent.
--
-- Kept although the table audit found no runtime reader or writer: dcn_chunks
-- and dcn_chunk_previews. They are the DCN chunk lane's designed schema
-- (v091) and carry the tier trigger scripts/check_dcn_cn0.py holds smoke to
-- (a machine-class preview is refused).

BEGIN;

ALTER TABLE tool_call_log ADD COLUMN IF NOT EXISTS project_id INTEGER;
CREATE INDEX IF NOT EXISTS idx_tool_call_log_project_at
    ON tool_call_log (project_id, at DESC) WHERE project_id IS NOT NULL;

-- A session's resolved weights were the simulated psyche's input; the
-- column stays for the durable voice/demo session rows and is no longer
-- required.
ALTER TABLE entity_sessions ALTER COLUMN resolved_weights DROP NOT NULL;

DROP TABLE IF EXISTS backup_key_platform_state CASCADE;
DROP TABLE IF EXISTS backup_ship_source CASCADE;
DROP TABLE IF EXISTS bug_reports CASCADE;
DROP TABLE IF EXISTS debug_reporters CASCADE;
DROP TABLE IF EXISTS catalog_installs CASCADE;
DROP TABLE IF EXISTS catalog_entries CASCADE;
DROP TABLE IF EXISTS channel_messages CASCADE;
DROP TABLE IF EXISTS channels CASCADE;
DROP TABLE IF EXISTS dpo_pairs CASCADE;
DROP TABLE IF EXISTS entity_rate_limits CASCADE;
DROP TABLE IF EXISTS lifecycle_hooks CASCADE;
DROP TABLE IF EXISTS network_audit CASCADE;
DROP TABLE IF EXISTS network_custom_services CASCADE;
DROP TABLE IF EXISTS network_service_state CASCADE;
DROP TABLE IF EXISTS notify_state CASCADE;
DROP TABLE IF EXISTS operator_audit_log CASCADE;
DROP TABLE IF EXISTS dispatch_log CASCADE;
DROP TABLE IF EXISTS entity_functions CASCADE;
DROP TABLE IF EXISTS entity_skills CASCADE;
DROP TABLE IF EXISTS entity_journal CASCADE;
DROP TABLE IF EXISTS entity_behavioral_log CASCADE;
DROP TABLE IF EXISTS benchmark_task_results CASCADE;
DROP TABLE IF EXISTS benchmark_run_attempts CASCADE;
DROP TABLE IF EXISTS benchmark_runs CASCADE;
DROP TABLE IF EXISTS codebook_snapshots CASCADE;
DROP TABLE IF EXISTS codebook_recommendations CASCADE;
DROP TABLE IF EXISTS coding_lessons CASCADE;

COMMIT;
