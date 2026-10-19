-- v075 — ARTIFACT primitive (Phase 6).
--
-- An ARTIFACT is a DISTINCT first-class thing from an APP. The host builds a
-- live artifact that is disposable but convertible to an app: fast,
-- lightweight, spun up in a default harness pod, stored as a project like an
-- app but not bound to the app SDK. The user can convert an artifact to an app
-- with the app SDK. The two terms are never conflated.
--
-- Storage decision (design fork b): an artifact is stored LIKE a project — it
-- reuses the `projects` row + its workspace at /var/lib/cseries/projects/<id> —
-- so import / index / the in-pod live-build preview all work for free. It is
-- kept DISTINCT from a project (and from an app) by a `kind` marker on the row.
-- A NEW table was rejected: it would fork the workspace-path machinery,
-- code-index, and ship pipeline that already key off projects(id). The marker
-- is the lightest change that preserves the distinction WITHOUT conflation.
--
--   kind='project'  (default) — a normal AppFactory project (existing behavior)
--   kind='artifact'           — a disposable, lightweight live build, NOT
--                               app-sdk-bound. App-convertible (see
--                               convert_artifact: flips kind back to 'project'
--                               after a provenance=custom app.yaml is written,
--                               then hands the SAME workspace to the App SDK
--                               via ship_project_service).
--
-- `disposable` (default true) marks an artifact the host may garbage-collect;
-- it flips to false the moment the artifact is converted to an app (it is no
-- longer disposable once it backs a real installed app).
--
-- Idempotent: re-running on a pod that already has the columns is a no-op
-- (ADD COLUMN IF NOT EXISTS + a guarded CHECK).

BEGIN;

ALTER TABLE projects
    ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'project';

ALTER TABLE projects
    ADD COLUMN IF NOT EXISTS disposable BOOLEAN NOT NULL DEFAULT false;

-- Constrain kind to the known set without breaking existing rows (all default
-- to 'project'). Guarded so re-applying is a no-op.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'projects_kind_check'
    ) THEN
        ALTER TABLE projects
            ADD CONSTRAINT projects_kind_check
            CHECK (kind IN ('project', 'artifact'));
    END IF;
END$$;

-- Artifacts are listed/swept by (entity, kind); index it.
CREATE INDEX IF NOT EXISTS idx_projects_entity_kind
    ON projects (entity_id, kind);

COMMIT;
