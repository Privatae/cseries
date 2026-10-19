-- v045: extend entity_behavioral_log.event_type CHECK to include
-- 'tool_result' so runtime/tools.py::_log_meta_tool_result rows can
-- actually persist.
--
-- A release advertised "tool call results visible in live view" via a
-- new event_type='tool_result' row written after each meta-tool dispatch.
-- The write silently failed for every row because the CHECK constraint
-- predates the new event_type. The try/except wrapper logged it at
-- debug level, so the failure was invisible — caught 2026-05-27 when
-- the live view still showed empty results.

BEGIN;

ALTER TABLE entity_behavioral_log
    DROP CONSTRAINT IF EXISTS entity_behavioral_log_event_type_check;

ALTER TABLE entity_behavioral_log
    ADD CONSTRAINT entity_behavioral_log_event_type_check
    CHECK (
        event_type IS NULL OR event_type IN (
            'drift',
            'violation',
            'reset',
            'debug_start',
            'debug_complete',
            'restore',
            'suspend',
            'escalation',
            'envelope_breach',
            'bot_execution',
            'tool_result',
            'skill_practiced',
            'skill_learned',
            'skill_outdated',
            'codebook_drift',
            'codebook_reset',
            'codebook_update'
        )
    );

COMMIT;
