-- v250 — when a limit last stopped work (runtime/user_limits.py).
--
-- Every limit that can stop work the person asked for is the person's setting.
-- When one binds, the enforcing code writes a row here, so
-- the Limits page in Settings and the Host's limits(op='status') can say when
-- it last stopped something and what. A note, never a gate: nothing reads it
-- to decide whether work runs.
--
-- Additive and idempotent; forward-only.

BEGIN;

CREATE TABLE IF NOT EXISTS limit_hits (
    id          BIGSERIAL   PRIMARY KEY,
    entity_id   INTEGER     NOT NULL,
    name        TEXT        NOT NULL,          -- user_limits.LIMITS key
    value       DOUBLE PRECISION,              -- the value in force; NULL = none was
    what        TEXT        NOT NULL DEFAULT '',   -- what it stopped, in words
    thread_key  TEXT,                           -- the conversation, when there was one
    at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_limit_hits_name_recent
    ON limit_hits (entity_id, name, at DESC);

COMMIT;
