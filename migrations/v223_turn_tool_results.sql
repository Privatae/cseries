-- v223: big tool results stay in the database (runtime/result_store.py).
--
-- A tool result over runtime/result_store.STORE_OVER_CHARS is written here
-- once, whole, when it arrives. The turn's message list carries its first part
-- and a handle (`res-<id>`), and the model reads on with
-- read(handle, offset, length). A result the turn cuts out after it was used
-- (context_budget.drop_used_tool_results) leaves the same handle behind, so
-- nothing the model has not read becomes unreachable.
--
--   turn_token    the running turn (session.py _turn_token, one per turn)
--   tool_call_id  the call that returned it; one row per call per turn
--   session_id    the session the turn ran in: the rows go when it is no
--                 longer open (session_handler.cleanup, the 15-minute loop
--                 that already ends idle sessions), with the turn's message
--                 list, the only thing that held their handles
--   content       the result as the tool returned it, serialized; never
--                 changed after it is written
--
-- v222 is taken by the pending-words branch (fix/typed-words-never-lost), so
-- this is v223.

BEGIN;

CREATE TABLE IF NOT EXISTS turn_tool_results (
    id            BIGSERIAL    PRIMARY KEY,
    entity_id     INTEGER      NOT NULL,
    session_id    TEXT,
    turn_token    TEXT         NOT NULL,
    tool_call_id  TEXT         NOT NULL,
    tool          TEXT         NOT NULL,
    chars         INTEGER      NOT NULL,
    content       TEXT         NOT NULL,
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT turn_tool_results_call_key UNIQUE (turn_token, tool_call_id)
);

CREATE INDEX IF NOT EXISTS idx_turn_tool_results_session
    ON turn_tool_results (session_id);

COMMIT;
