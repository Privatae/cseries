-- v185 — a task template carries the turn type it was written under.
--
-- A template is inert, but every task stamped from it runs its prompt. A
-- template written by a TYPED turn carries that type, so a task stamped from
-- it later — by schedule_task from an untyped turn, or by a reach-out runbook
-- firing — runs under that type, never with everything the Host holds. NULL
-- is a template written untyped: today's behaviour.

BEGIN;

ALTER TABLE entity_task_templates
    ADD COLUMN IF NOT EXISTS type_id UUID NULL REFERENCES turn_types(id) ON DELETE SET NULL;

COMMIT;
