-- v157 — tool_call_log: one row per tool call the model made, with the model
-- that made it and the gate's reason (2026-09-18).
--
-- WHY A NEW TABLE. Three ledgers already touch a tool call and none of them
-- answers the routing question "how often does <model> in <role> invent a
-- tool":
--   - entity_behavioral_log gets a gate-decision row and a result row per
--     meta-tool call (runtime/tools.py _log_meta_tool_decision /
--     _log_meta_tool_result), but neither carries the model, and the reason
--     is prose in `error`.
--   - error_events (v098) gets every non-ok tool result, keyed by tool, with
--     no model and no reason vocabulary.
--   - inference_cost_log has the model per CALL but knows nothing about the
--     tool calls that call produced.
-- session.py's own comment (2026-08-22) says it outright: "the entity DB has
-- no tool-call table (only inference_cost_log)". This is that table.
--
-- `reason` is the vocabulary runtime/tool_call_ledger.py declares and the
-- gates stamp on their envelopes — the CHECK below is the same list, so a
-- misspelt reason fails the INSERT (which the writer logs and swallows)
-- rather than landing as a new category nobody reads. Model-fault reasons
-- (unknown_name, not_advertised, bad_args_json, args_schema) are the
-- admission bench's headline metric; the rest are the platform's walls doing
-- their job and score nothing against the model.
--
-- `model` is the SERVED model for the round that produced the call, not the
-- routed one — a failover moves the model mid-turn (session.py `_served`).
--
-- Retention is the writer's problem, not this file's: rows are small and the
-- rollup reads a window. A prune can come with the first measured need.

CREATE TABLE IF NOT EXISTS tool_call_log (
    id            BIGSERIAL    PRIMARY KEY,
    at            TIMESTAMPTZ  NOT NULL DEFAULT now(),
    entity_id     INTEGER      NOT NULL,
    session_id    TEXT,
    lane          TEXT         NOT NULL CHECK (lane IN ('host', 'agent')),
    role          TEXT,
    provider      TEXT,
    model         TEXT,
    tool          TEXT         NOT NULL,
    reason        TEXT         NOT NULL CHECK (reason IN (
                      'ok', 'error', 'unknown_name', 'not_advertised',
                      'bad_args_json', 'args_schema', 'surface_denied',
                      'capability_denied', 'out_of_grant', 'policy_denied',
                      'blocked', 'dedup')),
    status        TEXT         NOT NULL,
    latency_ms    INTEGER,
    schema_errors JSONB        NOT NULL DEFAULT '[]'::jsonb,
    args          JSONB        NOT NULL DEFAULT '{}'::jsonb
);

-- The rollup's two reads: this entity's window, and one model's window.
CREATE INDEX IF NOT EXISTS idx_tool_call_log_entity_at
    ON tool_call_log (entity_id, at DESC);
CREATE INDEX IF NOT EXISTS idx_tool_call_log_model_at
    ON tool_call_log (model, at DESC);

DO $$
BEGIN
    IF to_regclass('public.tool_call_log') IS NOT NULL THEN
        COMMENT ON TABLE tool_call_log IS
            'One row per tool call the model made: the served model that made '
            'it and the gate''s machine-readable reason. Written by the host '
            'turn (lane host) and dispatched agents (lane agent) through '
            'runtime/tool_call_ledger.record; read by rollup() for the '
            'routing tool''s `measured` block and the admission bench.';
        COMMENT ON COLUMN tool_call_log.reason IS
            'runtime/tool_call_ledger.REASONS. unknown_name / not_advertised / '
            'bad_args_json / args_schema are the model''s fault (a call the '
            'surface could not take); the rest are platform walls.';
        COMMENT ON COLUMN tool_call_log.model IS
            'The SERVED model for the round that produced the call, not the '
            'routed one — a failover moves the model mid-turn.';
    END IF;
END $$;
