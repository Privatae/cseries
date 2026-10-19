-- v213: a task run's threads — the multi-threaded Host.
--
-- `threads` is how many threads the Host asked to run at once: each round of
-- the run starts that many thread turns, or as many as the Host's memory holds
-- then (runtime/host_memory), whichever is fewer. 1 is the serial run of v209.
-- No upper bound: the limit is not set by the platform; the memory is the
-- Host's to manage.

BEGIN;

ALTER TABLE task_runs ADD COLUMN IF NOT EXISTS threads INTEGER NOT NULL DEFAULT 1;
ALTER TABLE task_runs DROP CONSTRAINT IF EXISTS task_runs_threads_positive;
ALTER TABLE task_runs ADD CONSTRAINT task_runs_threads_positive CHECK (threads >= 1);

COMMIT;
