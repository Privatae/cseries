-- v209: a task's serial run.
--
-- One row per run of a task's work in serial mode: one Host turn at a time,
-- each started by the platform (runtime/task_serial.py) with a brief built
-- from the task record (v208), not from the previous turn's transcript.
--
--   status       running -> finished (every check answered or unanswerable),
--                stalled (STALL_TURNS turns in a row wrote nothing to the
--                record), or stopped (the Host's task_record op='stop', or
--                the background budget stayed full).
--   turns        turns the run has started that landed.
--   stall_turns  landed turns in a row that wrote nothing to the record; any
--                write (a unit found, claimed or covered, a finding, a check
--                set) starts it again at 0.
--   app_id       where the turns run: the project thread's app.
--   request      the person's request the run answers, carried into every
--                turn's brief.
--
-- One running run per project (the partial unique index): a second `run`
-- while one is running resumes it rather than starting another.

BEGIN;

CREATE TABLE IF NOT EXISTS task_runs (
    id              BIGSERIAL PRIMARY KEY,
    entity_id       INTEGER NOT NULL
                    REFERENCES entity_registry(id) ON DELETE CASCADE,
    project_id      INTEGER NOT NULL
                    REFERENCES projects(id) ON DELETE CASCADE,
    app_id          TEXT NOT NULL DEFAULT 'workbench',
    request         TEXT NOT NULL DEFAULT '',
    mode            TEXT NOT NULL DEFAULT 'serial'
                    CHECK (mode IN ('serial')),
    status          TEXT NOT NULL DEFAULT 'running'
                    CHECK (status IN ('running', 'finished', 'stalled', 'stopped')),
    turns           INTEGER NOT NULL DEFAULT 0,
    stall_turns     INTEGER NOT NULL DEFAULT 0,
    end_reason      TEXT,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at        TIMESTAMPTZ
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_task_runs_one_running
    ON task_runs (entity_id, project_id)
    WHERE status = 'running';

COMMIT;
