-- v002: chat_events audit log
--
-- One row per phase of a chat request. The April-25 → May-18 freeze that
-- swallowed 1,300+ "your message disappeared" failures would have shown
-- up as a single query against this table: which request_id last had a
-- `save_committed` row, and how many `error` rows since.
--
-- Append-only. Never deleted. Truncate manually if storage becomes an
-- issue (the schema_migrations row stays, so no migration re-runs).

CREATE TABLE IF NOT EXISTS chat_events (
    id           BIGSERIAL    PRIMARY KEY,
    request_id   UUID         NOT NULL,
    entity_id    INTEGER      NOT NULL,
    session_id   UUID,
    target_kind  TEXT,                            -- self | dm | entity | circle
    phase        TEXT         NOT NULL,           -- received | runtime_check | session_resolved | runtime_call | save_committed | returned
    status       TEXT         NOT NULL,           -- ok | error | warn
    text_preview TEXT,                            -- first ~120 chars of user msg, hashed if PII concerns
    error_class  TEXT,                            -- e.g. runtime_unavailable, session_open_failed, inference_no_data
    error_detail TEXT,
    elapsed_ms   INTEGER,
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_chat_events_request
    ON chat_events(request_id);
CREATE INDEX IF NOT EXISTS idx_chat_events_entity_time
    ON chat_events(entity_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_chat_events_error
    ON chat_events(error_class, created_at DESC)
    WHERE status = 'error';
