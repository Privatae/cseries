-- v073 — Scheduled-task TEMPLATES / runbooks (s1-runbook).
--
-- A template is a reusable runbook the host can stamp out into a scheduled
-- task. schedule_task (op='create', template_id=<id>) loads the template
-- (entity-scoped), renders its prompt + steps into the new scheduled_task's
-- payload, and inherits the template's recurrence if the caller didn't pass
-- one. The offline_task_runner then runs payload.steps IN SEQUENCE (one
-- governed turn per step), or the single prompt if there are no steps.
--
-- Managed by the host tool `task_template` (ops create/list/delete), strictly
-- entity-scoped. Idempotent so re-running on a pod that already has the table
-- is a no-op.

BEGIN;

CREATE TABLE IF NOT EXISTS entity_task_templates (
    id          SERIAL PRIMARY KEY,
    entity_id   INTEGER NOT NULL,
    name        TEXT NOT NULL,
    prompt      TEXT,
    steps       JSONB NOT NULL DEFAULT '[]',
    recurrence  VARCHAR(50),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_entity_task_templates_entity
    ON entity_task_templates (entity_id, created_at DESC);

COMMIT;
