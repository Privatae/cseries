-- v098: error_events — the pod-local error lane (2026-07-23).
--
-- Failed tool calls should not be on show; errors are logged instead, so they
-- can be diagnosed quickly without spending tokens finding them. Before this,
-- tool failures had exactly one durable trace:
-- session.py's call into session_manifest.record_error — a module ABSENT
-- from the entity image (import-guarded, silently dead), so every failure
-- vanished with the container logs (entity namespaces are deliberately NOT
-- shipped to central Loki — sovereignty: entity logs are user data; the
-- 2026-07-23 recall-failure logs died in a pod roll for exactly this reason).
--
-- error_events lives in the entity's OWN pod-PG (survives restarts on the
-- pgdata volume, stays inside the sovereignty boundary) and is written by
-- ONE recorder (server/error_log.py) from every choke point that sees a
-- failure: the open-lane tool loop, the SDK-lane host-scope wrappers, the
-- turn registry (crash/timeout), and the stream layer. Read it via
-- GET /api/system/errors/recent — diagnosis starts THERE, not in log spelunking.
--
-- Retention is enforced on write by the recorder (30 days), not by a cron.
-- init-db.sql carries the same definition for fresh pods (the v090 lesson:
-- a migration must produce exactly what init-db produces).

BEGIN;

CREATE TABLE IF NOT EXISTS error_events (
    id      BIGSERIAL PRIMARY KEY,
    at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    source  TEXT NOT NULL,                    -- 'tool' | 'sdk_tool' | 'turn' | 'stream'
    kind    TEXT NOT NULL DEFAULT 'error',    -- 'error' | 'denied' | 'timeout' | 'crash'
    tool    TEXT,                             -- tool name when a tool call failed
    message TEXT NOT NULL,                    -- error text (recorder truncates)
    context JSONB NOT NULL DEFAULT '{}'::jsonb -- session_id, turn_id, app_id, args preview
);

CREATE INDEX IF NOT EXISTS idx_error_events_at ON error_events (at DESC);

COMMIT;
