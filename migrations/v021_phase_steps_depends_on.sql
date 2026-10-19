-- v021 — add phase_steps.depends_on so the orchestrator's _check_dependencies
-- query works on pods provisioned before this column existed in init-db.sql.
-- project_orchestrator.py:135 queries `unnest(depends_on::int[])` and was
-- 500ing with UndefinedColumnError on every execute-next call against
-- existing pods.

ALTER TABLE phase_steps
    ADD COLUMN IF NOT EXISTS depends_on INTEGER[] DEFAULT '{}';
