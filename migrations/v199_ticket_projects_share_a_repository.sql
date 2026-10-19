-- v199 (2026-10-03): ticket projects share a Code Repository, each on its own branch.
--
-- Every ticket is its own project, and many tickets are
-- about one repository (the platform's own, most of all). Chosen: "shared
-- repo, own branch" — a ticket's project (projects.config.ticket, or
-- config.support_ticket from runtime/support_collect) mounts the repository
-- the ticket is about and works there on a branch named for the ticket, with
-- its own Code Approval (server/routes/projects.mount_ticket_repo_service).
--
-- So v191's one-live-project-per-repository index now leaves ticket projects
-- out: the rule still binds every other project among themselves, exactly as
-- v191 set it. What keeps two projects in one repository apart
-- is the branch, so a second index refuses two live projects on one branch of
-- one repository.
--
-- An entity whose rows already break either rule keeps running: that index
-- is not created there and a NOTICE says so, as in v191. The narrower
-- repository index replaces v191's only where it can be created.

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM projects
         WHERE repo IS NOT NULL AND status <> 'archived'
           AND NOT (COALESCE(config, '{}'::jsonb) ? 'ticket'
                    OR COALESCE(config, '{}'::jsonb) ? 'support_ticket')
         GROUP BY entity_id, lower(repo)
        HAVING COUNT(*) > 1
    ) THEN
        RAISE NOTICE 'v199: live projects already share a repository here; '
                     'uq_projects_live_repo left as it was';
    ELSE
        DROP INDEX IF EXISTS uq_projects_live_repo;
        CREATE UNIQUE INDEX uq_projects_live_repo
            ON projects (entity_id, lower(repo))
            WHERE repo IS NOT NULL AND status <> 'archived'
              AND NOT (COALESCE(config, '{}'::jsonb) ? 'ticket'
                       OR COALESCE(config, '{}'::jsonb) ? 'support_ticket');
    END IF;

    IF EXISTS (
        SELECT 1 FROM projects
         WHERE repo IS NOT NULL AND status <> 'archived'
           AND COALESCE(config, '{}'::jsonb) ? 'work_branch'
         GROUP BY entity_id, lower(repo), config->>'work_branch'
        HAVING COUNT(*) > 1
    ) THEN
        RAISE NOTICE 'v199: live projects already share a work branch here; '
                     'uq_projects_live_repo_branch not created';
    ELSE
        CREATE UNIQUE INDEX IF NOT EXISTS uq_projects_live_repo_branch
            ON projects (entity_id, lower(repo), (config->>'work_branch'))
            WHERE repo IS NOT NULL AND status <> 'archived'
              AND COALESCE(config, '{}'::jsonb) ? 'work_branch';
    END IF;
END $$;
