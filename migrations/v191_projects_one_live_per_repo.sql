-- v191 — one live project per Code Repository.
--
-- A project mounts one repository, and two live projects never share one:
-- a repository that already has a project is offered as a fork into the new
-- project instead. The routes check first and answer
-- with that offer; this index is the database's own refusal behind them, so
-- a race or a path that skips the check still cannot make a second live
-- project on a repository.
--
-- Archived projects are not live: archiving frees the repository, and a
-- restore that would make a second live project on it is refused by the
-- same index (projects.update_project_service answers 409).
--
-- An entity that already has two live projects on one repository keeps
-- running: the index is not created there, a NOTICE says so, and the routes'
-- own check still refuses every new pair. Nothing here archives or rewrites a
-- project to make the index fit.

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM projects
         WHERE repo IS NOT NULL AND status <> 'archived'
         GROUP BY entity_id, lower(repo)
        HAVING COUNT(*) > 1
    ) THEN
        RAISE NOTICE 'v191: live projects already share a repository here; '
                     'uq_projects_live_repo not created';
    ELSE
        CREATE UNIQUE INDEX IF NOT EXISTS uq_projects_live_repo
            ON projects (entity_id, lower(repo))
            WHERE repo IS NOT NULL AND status <> 'archived';
    END IF;
END $$;
