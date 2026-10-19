-- v226: work items, the shared record of parallel work started
-- from the chat (runtime/task_record.py
-- op='run' with items, runtime/task_serial.py).
--
-- A task run of kind 'work' runs one internal thread per work item: the Host
-- may start one from any conversation, without asking, for work that splits
-- (two tickets in one message). Each thread owns exactly one item and writes
-- its state there: its status, the step it is on, the links it made and what
-- it found. At every turn start a thread reads its siblings' items, never
-- their turns. A thread that finishes or is blocked posts one line into the
-- conversation the run was started from (task_runs.origin), once
-- (posted_at). A 'research' run is every run before this one: goal checks,
-- coverage and findings (v208), unchanged.
--
-- Not mirrored in init-db.sql: task_runs (v209) and projects come from
-- migrations that file does not mirror either, and this table references both.

BEGIN;

ALTER TABLE task_runs ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'research';
ALTER TABLE task_runs DROP CONSTRAINT IF EXISTS task_runs_kind_known;
ALTER TABLE task_runs ADD CONSTRAINT task_runs_kind_known CHECK (kind IN ('research', 'work'));
-- Where the run was started: {thread_id, interaction_context, project_id,
-- session_id}, the shape offline_task_runner.post_to_origin_thread takes.
ALTER TABLE task_runs ADD COLUMN IF NOT EXISTS origin JSONB;

CREATE TABLE IF NOT EXISTS task_work_items (
    id          BIGSERIAL PRIMARY KEY,
    entity_id   INTEGER NOT NULL
                REFERENCES entity_registry(id) ON DELETE CASCADE,
    project_id  INTEGER NOT NULL
                REFERENCES projects(id) ON DELETE CASCADE,
    run_id      BIGINT NOT NULL
                REFERENCES task_runs(id) ON DELETE CASCADE,
    thread_no   INTEGER NOT NULL CHECK (thread_no >= 0),
    goal        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'open'
                CHECK (status IN ('open', 'working', 'blocked', 'done')),
    step        TEXT,
    links       JSONB NOT NULL DEFAULT '[]'::jsonb,
    findings    JSONB NOT NULL DEFAULT '[]'::jsonb,
    outcome     TEXT,
    posted_at   TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_task_work_items_thread UNIQUE (run_id, thread_no)
);

CREATE INDEX IF NOT EXISTS idx_task_work_items_live
    ON task_work_items (entity_id, status);

COMMIT;
