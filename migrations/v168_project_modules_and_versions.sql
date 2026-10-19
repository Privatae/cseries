-- v168 — project_modules + module_versions; project_phases.module_id.
--
-- A project holds several MODULES (front end, back end,
-- private app, public site, store app), each a directory of the project's
-- one tree (<project>/<module>/, its own app.yaml at its root: one tree
-- with a directory per module). A landed run is
-- promoted into a module as a VERSION: a commit on the tree, recorded here;
-- the module's current version is a POINTER (current_version_id), never a
-- rewrite in place. Undo moves the pointer; no version row is ever deleted.
--
-- target      what the module ships as, chosen at alignment; it decides the
--             contract the builder is handed with the brief
--             (server/applications/build_contract.contract_for_target):
--             private_app | public_site | store_app, or component — a piece
--             of another module (a back end) that ships as nothing itself.
-- validation  how the module's phases are proven, required at plan time:
--             checks (acceptance commands, non-empty per phase), proof (the
--             platform starts it and polls — installable targets only) or
--             review (the user looks).
--
-- A project with no module rows keeps today's single-app root layout;
-- nothing is migrated (the no-migration rule).
--
-- entity_id is not repeated here: a module is reached through its project,
-- which is entity-scoped (projects.entity_id). current_version_id's FK is
-- added after module_versions exists (the two tables reference each other).
--
-- Mirrored in init-db.sql for fresh entities. Idempotent, forward-only.

CREATE TABLE IF NOT EXISTS project_modules (
    id                  SERIAL PRIMARY KEY,
    project_id          INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name                TEXT NOT NULL
                        CHECK (name ~ '^[a-z0-9][a-z0-9-]{0,39}$'),
    target              TEXT NOT NULL
                        CHECK (target IN ('private_app','public_site','store_app','component')),
    validation          TEXT NOT NULL
                        CHECK (validation IN ('checks','proof','review')),
    description         TEXT NOT NULL DEFAULT '',
    current_version_id  INTEGER,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (project_id, name)
);

CREATE TABLE IF NOT EXISTS module_versions (
    id          SERIAL PRIMARY KEY,
    module_id   INTEGER NOT NULL REFERENCES project_modules(id) ON DELETE CASCADE,
    number      INTEGER NOT NULL,
    -- The numbered run it came from (harness_run.id), when it came from one.
    run_id      INTEGER,
    -- The save point on the project tree that IS this version.
    commit_sha  TEXT,
    source      TEXT NOT NULL CHECK (source IN ('build','adopt')),
    note        TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Set when an undo moves the pointer off this version (or clears it);
    -- undo never lands on an undone version. Cleared when the pointer is
    -- moved back onto it by hand.
    undone_at   TIMESTAMPTZ,
    UNIQUE (module_id, number)
);

-- A database that applied an earlier draft of this migration.
ALTER TABLE module_versions ADD COLUMN IF NOT EXISTS undone_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_module_versions_module ON module_versions (module_id, number DESC);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'project_modules_current_version_fk'
    ) THEN
        ALTER TABLE project_modules
            ADD CONSTRAINT project_modules_current_version_fk
            FOREIGN KEY (current_version_id) REFERENCES module_versions(id) ON DELETE SET NULL;
    END IF;
END$$;

-- A phase belongs to a module when the plan named one; NULL = the project's
-- root app (every plan written before modules).
ALTER TABLE project_phases
    ADD COLUMN IF NOT EXISTS module_id INTEGER REFERENCES project_modules(id) ON DELETE SET NULL;

-- How THIS phase is proven, written when the phase becomes buildable (plan,
-- add_phase or set_phase_spec with a brief): checks (it carries acceptance),
-- proof or review. NULL = a phase written before v168, which builds exactly
-- as it always did.
ALTER TABLE project_phases
    ADD COLUMN IF NOT EXISTS validation TEXT;
ALTER TABLE project_phases DROP CONSTRAINT IF EXISTS project_phases_validation_check;
ALTER TABLE project_phases ADD CONSTRAINT project_phases_validation_check
    CHECK (validation IS NULL OR validation IN ('checks','proof','review'));
