-- v242 — `host_restore`: the role a restore loads a backup's rows as
-- (server/host_restore.py _load_rows).
--
-- Made here, by the boot's schema step, so the kernel never creates a role
-- while it runs (scripts/test_no_runtime_ddl.py). A restore builds its own
-- database with this same step (host_restore._build_own_schema) before it
-- loads anything, so the role is there for every load, on a fresh Host too.
--
-- It is made unable to log in and holding nothing. A restore gives it a login
-- and its grants in the restore's own database for the load and takes them back
-- after (host_restore._load_rows); every restore starts by locking it
-- (host_restore.lock_load_role), and so does every start of the database
-- container (entrypoint-postgres.sh lock_restore_role).
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'host_restore') THEN
        CREATE ROLE host_restore NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION
            NOBYPASSRLS NOINHERIT;
    END IF;
END $$;
