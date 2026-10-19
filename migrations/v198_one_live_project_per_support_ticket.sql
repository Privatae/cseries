-- v198 (2026-10-01): one live project per support ticket.
--
-- Each support ticket is its own project, carrying the
-- ticket's number (projects.config.support_ticket, written by
-- runtime/support_collect._ticket_project). The code looks the project up
-- before creating it; this index is the database's refusal behind that
-- look-up, so two passes deciding at the same moment cannot make two
-- projects for one ticket (the code re-reads on the violation). Archived
-- projects are not live: archiving one frees the ticket for a new project.
--
-- An entity that already has two live projects for one ticket keeps running:
-- the index is not created there and a NOTICE says so (none exist at the
-- time of writing — the support path had never opened a project).

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM projects
         WHERE config ? 'support_ticket' AND status <> 'archived'
         GROUP BY entity_id, config->>'support_ticket'
        HAVING COUNT(*) > 1
    ) THEN
        RAISE NOTICE 'v198: live projects already share a support ticket here; '
                     'uq_projects_live_support_ticket not created';
    ELSE
        CREATE UNIQUE INDEX IF NOT EXISTS uq_projects_live_support_ticket
            ON projects (entity_id, (config->>'support_ticket'))
            WHERE config ? 'support_ticket' AND status <> 'archived';
    END IF;
END $$;
