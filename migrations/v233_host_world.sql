-- v233 — `host_world`: the Host's own schema, and the role that owns it.
--
-- WHAT THIS IS FOR. The Host builds its own storage: the tables, views and
-- functions a request needs, in the schema `world`, through the `data` tool. The
-- tool runs the model's SQL as `host_world`. Postgres decides what that role can
-- touch, so the wall holds whatever the model writes:
--   - it owns `world` and can create, change and drop anything there;
--   - it reads only the kernel tables on the READABLE list below;
--   - it writes nothing outside `world`, holds no CREATE on the database (so no
--     other schema and no extension), and cannot become another role.
-- The kernel's tables hold the person's approvals, the step-up seed, the sealed
-- keys and the log of every tool call. A Host that could write them could approve
-- its own egress or edit its own record. Everything else is its own.
--
-- RUN ON EVERY BOOT, AFTER THE MIGRATION LOOP, NOT BY IT. entrypoint.sh records
-- version 233 before the loop so the loop skips this file, then applies it once
-- every migration has run. The reasons are v139's (entrypoint.sh, "The recall
-- role, asserted on EVERY boot"): a role is cluster state and a restore brings
-- back neither the role nor its grants; a drifted role must be pulled back on the
-- next boot. It runs AFTER the loop so its grants reconcile against the whole
-- schema of this boot, including any table a migration created a moment ago.
-- Every statement below RECONCILES: privileges are reset before they are granted.
--
-- CLOSED BY DEFAULT. host_recall grants every table and revokes exceptions; a new
-- secret table is readable by it until someone adds the exception. That is
-- tolerable for recall's fixed queries and not for SQL the model writes, which can
-- send what it reads anywhere. So host_world gets no default privileges and reads
-- a kernel table only when it is on READABLE. scripts/test_host_world_pg.py fails
-- when a declared table is on no list, so a new table is a decision, and a miss
-- costs the Host a read, never a secret.

BEGIN;

-- ── The role ────────────────────────────────────────────────────────────────
-- CONNECTION LIMIT 3: the kernel's pool of 2, plus one for a snapshot or restore.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'host_world') THEN
        CREATE ROLE host_world
            LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION
            CONNECTION LIMIT 3;
        RAISE NOTICE 'v233: role host_world created';
    ELSE
        ALTER ROLE host_world
            LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION
            CONNECTION LIMIT 3;
    END IF;
END $$;

-- Membership in any role would let `SET ROLE` step around everything below
-- (v139 measured it). Revoke every membership, including one granted later.
DO $$
DECLARE r record;
BEGIN
    FOR r IN
        SELECT g.rolname AS grantee_of
          FROM pg_auth_members m
          JOIN pg_roles g ON g.oid = m.roleid
         WHERE m.member = (SELECT oid FROM pg_roles WHERE rolname = 'host_world')
    LOOP
        EXECUTE format('REVOKE %I FROM host_world', r.grantee_of);
        RAISE NOTICE 'v233: revoked host_world''s membership in %', r.grantee_of;
    END LOOP;
END $$;

-- Session defaults. temp_file_limit only a superuser may change, so the Host's SQL
-- cannot raise it. The others are defaults: the `data` tool's own timeout, which
-- cancels from outside the session, is the bound that holds.
ALTER ROLE host_world RESET ALL;
-- pg_temp is named LAST on purpose. Left out, a session's temporary schema is
-- searched FIRST, before pg_catalog, so a temporary table named pg_class would
-- shadow the catalog the kernel's executor reads, for as long as the pooled
-- connection lives. Named, it is searched where it is listed.
ALTER ROLE host_world SET search_path = world, public, pg_temp;
ALTER ROLE host_world SET statement_timeout = '60s';
ALTER ROLE host_world SET lock_timeout = '5s';
ALTER ROLE host_world SET idle_in_transaction_session_timeout = '120s';
ALTER ROLE host_world SET temp_file_limit = '1GB';

-- ── Its schema ──────────────────────────────────────────────────────────────
CREATE SCHEMA IF NOT EXISTS world AUTHORIZATION host_world;
ALTER SCHEMA world OWNER TO host_world;
REVOKE ALL ON SCHEMA world FROM PUBLIC;
COMMENT ON SCHEMA world IS
    'The Host''s own storage: tables, views and functions it built for the person. '
    'Owned by host_world. Every object carries a COMMENT saying what it is for.';

-- Ownership comes back to host_world. A restore made with --no-owner leaves what
-- it loaded owned by whoever loaded it, and the Host could no longer change what
-- it made. Tables carry their indexes, sequences owned by a column and row types
-- with them.
DO $$
DECLARE r record;
BEGIN
    FOR r IN
        SELECT format('%I.%I', n.nspname, c.relname) AS obj,
               CASE c.relkind WHEN 'v' THEN 'VIEW' WHEN 'm' THEN 'MATERIALIZED VIEW'
                              WHEN 'S' THEN 'SEQUENCE' WHEN 'f' THEN 'FOREIGN TABLE'
                              ELSE 'TABLE' END AS kind
          FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = 'world' AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f')
           AND c.relowner <> 'host_world'::regrole
           AND NOT (c.relkind = 'S' AND EXISTS (
                   SELECT 1 FROM pg_depend d
                    WHERE d.objid = c.oid AND d.classid = 'pg_class'::regclass
                      AND d.deptype IN ('a', 'i')))
    LOOP
        EXECUTE format('ALTER %s %s OWNER TO host_world', r.kind, r.obj);
    END LOOP;
    FOR r IN
        SELECT p.oid::regprocedure::text AS obj,
               CASE p.prokind WHEN 'p' THEN 'PROCEDURE' WHEN 'a' THEN 'AGGREGATE'
                              ELSE 'FUNCTION' END AS kind
          FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
         WHERE n.nspname = 'world' AND p.proowner <> 'host_world'::regrole
    LOOP
        EXECUTE format('ALTER %s %s OWNER TO host_world', r.kind, r.obj);
    END LOOP;
    FOR r IN
        SELECT format('%I.%I', n.nspname, t.typname) AS obj,
               CASE t.typtype WHEN 'd' THEN 'DOMAIN' ELSE 'TYPE' END AS kind
          FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
         WHERE n.nspname = 'world' AND t.typowner <> 'host_world'::regrole
           AND t.typrelid = 0
           AND NOT EXISTS (SELECT 1 FROM pg_type b WHERE b.typarray = t.oid)
    LOOP
        EXECUTE format('ALTER %s %s OWNER TO host_world', r.kind, r.obj);
    END LOOP;
END $$;

-- ── Emptying `world`, for a restore ──────────────────────────────────
-- A restore loads a snapshot into an EMPTY world, or tables made after the
-- snapshot would stay. The statements that empty it live here, in declared SQL,
-- so the kernel's executor holds no DDL text of its own. SECURITY
-- INVOKER, and it refuses any role but host_world: it drops only what that role
-- owns, in the one schema it owns. Owned by entity, in a schema the Host cannot
-- write, so the Host cannot change what a restore runs.
CREATE SCHEMA IF NOT EXISTS world_admin;
REVOKE ALL ON SCHEMA world_admin FROM PUBLIC;
GRANT USAGE ON SCHEMA world_admin TO host_world;

CREATE OR REPLACE PROCEDURE world_admin.empty_world()
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog
AS $$
DECLARE
    stmt text;
    stmts text[];
BEGIN
    IF current_user <> 'host_world' THEN
        RAISE EXCEPTION 'world_admin.empty_world runs only as host_world, not %', current_user;
    END IF;
    -- Names are taken before anything is dropped: a CASCADE can remove a later
    -- object, and IF EXISTS then skips it.
    SELECT array_agg(s) INTO stmts FROM (
        SELECT format('DROP %s IF EXISTS %I.%I CASCADE',
                      CASE c.relkind WHEN 'v' THEN 'VIEW' WHEN 'm' THEN 'MATERIALIZED VIEW'
                                     WHEN 'S' THEN 'SEQUENCE' WHEN 'f' THEN 'FOREIGN TABLE'
                                     ELSE 'TABLE' END,
                      n.nspname, c.relname) AS s
          FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = 'world' AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f')
        UNION ALL
        SELECT format('DROP %s IF EXISTS %s CASCADE',
                      CASE p.prokind WHEN 'p' THEN 'PROCEDURE' WHEN 'a' THEN 'AGGREGATE'
                                     ELSE 'FUNCTION' END,
                      p.oid::regprocedure::text)
          FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
         WHERE n.nspname = 'world'
        UNION ALL
        SELECT format('DROP %s IF EXISTS %I.%I CASCADE',
                      CASE t.typtype WHEN 'd' THEN 'DOMAIN' ELSE 'TYPE' END,
                      n.nspname, t.typname)
          FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
         WHERE n.nspname = 'world' AND t.typrelid = 0
           AND NOT EXISTS (SELECT 1 FROM pg_type b WHERE b.typarray = t.oid)
    ) q;
    IF stmts IS NULL THEN
        RETURN;
    END IF;
    FOREACH stmt IN ARRAY stmts LOOP
        EXECUTE stmt;
    END LOOP;
END $$;

REVOKE ALL ON PROCEDURE world_admin.empty_world() FROM PUBLIC;
GRANT EXECUTE ON PROCEDURE world_admin.empty_world() TO host_world;

-- ── Reset, then grant ───────────────────────────────────────────────────────
REVOKE ALL ON SCHEMA public FROM host_world;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM host_world;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM host_world;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM host_world;
ALTER DEFAULT PRIVILEGES FOR ROLE entity IN SCHEMA public
    REVOKE ALL ON TABLES FROM host_world;
ALTER DEFAULT PRIVILEGES FOR ROLE entity IN SCHEMA public
    REVOKE ALL ON SEQUENCES FROM host_world;

-- Only public's owner creates in public. PostgreSQL 15 made this the default for
-- a new cluster; a cluster first made by an older server keeps PUBLIC's CREATE,
-- and a role that can create in public can shadow a kernel table the kernel
-- has not created yet. Nothing on this database relies on PUBLIC's CREATE: entity
-- owns public, and the other two roles must not create there.
REVOKE CREATE ON SCHEMA public FROM PUBLIC;

-- No CREATE on the database: no schema outside `world`, no extension. PUBLIC does
-- not hold it by default; revoked anyway so a hand grant does not survive a boot.
DO $$
BEGIN
    EXECUTE format('REVOKE CREATE ON DATABASE %I FROM PUBLIC', current_database());
    EXECUTE format('REVOKE CREATE ON DATABASE %I FROM host_world', current_database());
END $$;

GRANT USAGE ON SCHEMA public TO host_world;

-- ── The three lists ──────────────────────────────────────────────────
-- Every table the kernel declares (init-db.sql, migrations/) is on exactly one.
-- scripts/test_host_world_pg.py builds the declared schema and fails on a table
-- that is on none, or on two.
--
-- SECRET — key material, bearer values, sealed or encrypted secrets. Never
-- readable by host_world, and revoked from host_recall below (v139 revokes only
-- the first two, and only before the migration loop). An audit found:
--   kernel_secrets          every private key the Host holds (v139:199-206)
--   nac_agent_delegations   a plaintext bearer value (v139:210-215)
--   nac_agent_activity      the same bearer value copied per agent
--                           (v061_nac_agents.sql:21; nac_agents.py upsert)
--   vault_envelope          every stored credential, sealed to a key in this database
--   local_backup_envelope   the wrapped data key of a local backup
--   local_backup_blob       the encrypted backup, and a hash of its plaintext
--   backup_user_key         the person's backup key, sealed to a passphrase (v106)
--   backup_ship_source      a signed download URL, a bearer until it expires (v107)
--   nac_connector_grants    the hash of a short pairing code, guessable offline
--   nac_connector_caps      the hashes of connector capability codes
--   owner_account           the owner's sign-in: the scrypt hash of the password (v228)
--   owner_sessions          the SHA-256 of every session cookie (v228)
--   mcp_oauth_pending       an MCP sign-in in flight: its PKCE verifier (v239)
--   owner_passkeys          the owner's passkeys: who can sign in to this Host (v240)
--   owner_recovery_codes    the SHA-256 of each of the owner's recovery codes (v240)
--
-- CLOSED — not shown to be a secret, not shown to be safe. Unreadable until a
-- reason moves it to READABLE or SECRET:
--   app_config              free-form JSON any app writes
--   host_backup_state       backup bookkeeping (key/value)
--   host_backup_runs        backup runs; sidecar_key names the sealed per-backup key
--   entity_registry         the Host's registry row; identity_lock is unexplained
--   mcp_oauth               an MCP server's sign-in state (v239): no token is in it
--                           (they are vault entries), and the Host reads the state
--                           through mcp_connections(op='servers'), not SQL
--
-- READABLE — the Host's own records with no secret in them: its conversations,
-- memory, documents, projects, journal, tool log, network rules, apps and the rest.
-- (The simulated psyche's tables and the batch consolidator's ledger left this
-- list with migrations/v235_one_memory.sql.)
DO $$
DECLARE
    t text;
    readable text[] := ARRAY[
       'app_store_circle_publications', 'app_token_audit', 'application_signing_keys',
       'backup_ship_log', 'bastion_autoupdate_settings',
       'bastion_autoupdate_state', 'bastion_autoupdate_watch',
       'bastion_pairing_tokens', 'bastion_update_refusals', 'behavioral_corrections',
       'browser_form_grant', 'calendar_event_runs', 'calendar_events',
       'chat_events', 'circles_page_marks',
       'circles_thread_links', 'circles_threads', 'code_chunks', 'code_repos',
       'config_versions', 'connector_messages',
       'contacts', 'context_backend_locations', 'context_backend_meta',
       'dcn_chunk_previews', 'dcn_chunks', 'dcn_files', 'dcn_move_plans', 'dcn_stores',
       'decision_patterns', 'decisions', 'domain_policy', 'domain_registry',
       'embed_jobs', 'entity_appearance',
       'entity_context', 'entity_domain_access',
       'entity_links', 'entity_memory',
       'entity_permissions', 'entity_sessions', 'entity_task_templates', 'entity_tasks', 'entity_tickets', 'error_events',
       'generated_images', 'gitea_propose_audit', 'goal_checks', 'group_app_grants',
       'group_ballots', 'group_join_requests', 'group_members', 'group_outbox',
       'group_slips', 'group_votes', 'groups', 'harness_run', 'harness_usage',
       'harness_usage_event', 'held_slips', 'host_op_runs', 'host_skills',
       'inbox_messages', 'inference_cost_log', 'installed_applications',
       'integration_connections', 'integration_grants', 'integration_oauth_clients',
       'integration_records', 'integration_store_retired', 'integration_syncs',
       'live_context_cards', 'mcp_client_tokens', 'mcp_connections',
       'memory_audit_findings', 'model_bench_runs', 'model_route_log',
       'module_versions', 'nac_agent_audit', 'nac_agent_contain',
       'nac_bastion_quarantine', 'nac_bastion_suspend', 'nac_egress', 'nac_endpoints',
       'nac_finding_contain', 'nac_mgmt_audit', 'nac_mgmt_grants',
       'nac_pairing_grants', 'nac_policy', 'nac_profiles', 'network_bastion_heartbeat', 'network_bastion_other', 'network_bastion_pairing',
       'network_bastion_peer', 'network_machine_map',
       'network_machine_offers', 'oversight_findings', 'oversight_store_cursor',
       'peer_app_inbox', 'peer_app_outbox', 'peer_app_slips', 'peer_cards',
       'pending_words', 'phase_steps', 'project_folders', 'project_modules',
       'project_phases', 'project_routing', 'projects', 'prompt_log',
       'reachout_channels', 'reachout_log', 'reachout_triggers',
       'rendezvous_interests', 'rendezvous_state', 'research_chunks', 'research_pages',
       'schema_migrations', 'service_reach_grants', 'ship_machine_keys',
       'ship_pipelines', 'ship_runs', 'ship_steps', 'step_provenance',
       'support_tickets', 'task_coverage', 'task_findings', 'task_runs',
       'task_sources', 'task_work_items', 'tool_call_log',
       'turn_tool_results', 'turn_types', 'ui_last_open', 'update_notifications',
       'user_context', 'vault_audit', 'vault_audit_rollup', 'vault_identity_pubkey',
       'world_catalog_vectors', 'memory_recall_aliases'
    ];
    secret text[] := ARRAY[
       'kernel_secrets', 'nac_agent_delegations', 'nac_agent_activity', 'vault_envelope',
       'local_backup_envelope', 'local_backup_blob', 'backup_user_key',
       'nac_connector_grants', 'nac_connector_caps',
       'owner_account', 'owner_sessions', 'mcp_oauth_pending',
       'owner_passkeys', 'owner_recovery_codes'
    ];
    closed text[] := ARRAY[
       'app_config', 'host_backup_state', 'host_backup_runs', 'entity_registry', 'mcp_oauth'
    ];
BEGIN
    FOREACH t IN ARRAY readable LOOP
        IF to_regclass(format('public.%I', t)) IS NOT NULL THEN
            EXECUTE format('GRANT SELECT ON public.%I TO host_world', t);
        END IF;
    END LOOP;
    -- host_recall's half of SECRET. Re-applied here, after the loop, because v139
    -- runs before it: a table a migration creates in the same boot gets
    -- host_recall's default SELECT, and only a revoke after the loop takes it back.
    FOREACH t IN ARRAY secret LOOP
        IF to_regclass(format('public.%I', t)) IS NOT NULL
           AND EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'host_recall') THEN
            EXECUTE format('REVOKE ALL ON public.%I FROM host_recall CASCADE', t);
        END IF;
    END LOOP;
    -- CLOSED and SECRET are never granted to host_world: the reset above revoked
    -- everything, and nothing below this line grants. Named here so a reader and
    -- the gate see all three lists in one place.
    PERFORM closed;
END $$;

COMMIT;
