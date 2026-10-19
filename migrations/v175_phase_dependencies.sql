-- v175: parallel builds.
--
-- depends_on — the phases this one waits for (project_phases ids). NULL = the
-- default: the phase before it in the same module (or among the root app's
-- phases), so every plan written before v175 builds in exactly the order it
-- always did. An empty array = waits for nothing. Auto-advance starts every
-- phase whose dependencies are complete, together.
--
-- harness / model — the engine this phase builds on, when the plan named one.
-- NULL = the engine the build was started with.
--
-- integration_error — set when this phase's run could not be merged with work
-- another phase landed while it was building: nothing of it landed, it is not
-- a coding failure and is not retried, and nothing starts it again on its own
-- until the plan is re-split (set_phase_spec, a new plan) or the user builds it
-- by name. NULL = no conflict.
--
-- Declared for fresh entities in init-db.sql (the project_phases block);
-- idempotent here for the long-lived ones.

ALTER TABLE project_phases ADD COLUMN IF NOT EXISTS depends_on INTEGER[];
ALTER TABLE project_phases ADD COLUMN IF NOT EXISTS harness TEXT;
ALTER TABLE project_phases ADD COLUMN IF NOT EXISTS model TEXT;
ALTER TABLE project_phases ADD COLUMN IF NOT EXISTS integration_error TEXT;
