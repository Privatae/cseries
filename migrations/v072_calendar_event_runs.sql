-- v072 — Offline-task executor (S1 SURGICAL) run-record table.
--
-- The offline_task_runner loop (brain/mastercode/brain/offline_task_runner.py)
-- claims a due calendar_events row (event_type='scheduled_task'), runs its
-- prompt through the governed host turn pipeline WITHOUT booting the full
-- autonomous brain, and appends one row here per execution attempt.
--
-- One scheduled task -> many runs (each recurrence occurrence is a run).
-- event_id references calendar_events.id (the integer SERIAL PK the loop
-- claims), NOT the UUID event_id column. Idempotent so re-running on a pod
-- that already has the table is a no-op.

BEGIN;

CREATE TABLE IF NOT EXISTS calendar_event_runs (
    id              SERIAL PRIMARY KEY,
    event_id        INTEGER NOT NULL
        REFERENCES calendar_events (id) ON DELETE CASCADE,
    entity_id       INTEGER NOT NULL,
    ran_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ok              BOOLEAN NOT NULL DEFAULT FALSE,
    output_excerpt  TEXT,
    error           TEXT
);

CREATE INDEX IF NOT EXISTS idx_calendar_event_runs_event
    ON calendar_event_runs (event_id, ran_at DESC);
CREATE INDEX IF NOT EXISTS idx_calendar_event_runs_entity
    ON calendar_event_runs (entity_id, ran_at DESC);

COMMIT;
