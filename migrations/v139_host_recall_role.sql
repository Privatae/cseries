-- v139 — `host_recall`: a second login role for the Host's READ path.
--
-- WHAT THIS IS FOR. Today the entity database has exactly one login role —
-- `entity`, the bootstrap superuser initdb creates (entrypoint-postgres.sh:241,
-- `initdb --auth=trust -U entity`). It carries rolsuper and rolbypassrls, so row
-- security is not weak on this database, it is INERT: a superuser bypasses RLS
-- unconditionally and this role additionally carries the explicit bypass flag.
-- Perfect policies would return every row. This migration creates the role that
-- policies can actually bind to.
--
-- WHY THE KERNEL KEEPS ITS SUPERUSER. `server/` carries 38 `CREATE TABLE IF NOT
-- EXISTS` statements across 23 modules — 100 runtime DDL statements once the
-- CREATE INDEX and ALTER TABLE … ADD COLUMN in the same blocks are counted
-- (the raw grep returns 40 and two of those are prose). The sites include vault/store.py,
-- vault/identity.py,
-- backup_key.py, backup_ship.py, browser_autofill.py, routes/embed.py,
-- routes/appearance.py, routes/compute.py, network/nac_agents.py,
-- network/topology.py, network/peer_discovery.py, network/nac_dcn.py and the
-- rest) — the Host creates its own tables on first use of a feature.
-- Downgrading `entity` would break those lazily: not at boot, but the first
-- time somebody opened backup, or autofill, or the appstore, months later, in
-- production, on one customer. So `entity` is untouched here. This is not "the
-- Host gets a weaker role"; it is "the AI's read path gets its OWN role".
--
-- THE SAFETY PROPERTY. After this migration alone, behaviour is observationally
-- IDENTICAL to today. No policy exists, so the restricted role sees exactly what
-- the superuser saw. That decoupling — plumbing now, policies later, one table at
-- a time with a query that proves what each hides — is the only reason this is
-- shippable with customers on the platform. DO NOT add an RLS policy to this
-- file.
--
-- READ-ONLY, AND ONLY READ-ONLY. `_read_pool` (memory_retrieval.py) has exactly
-- one consumer, `recall_memories`, and every statement it issues is a SELECT.
-- Measured rather than asserted: with log_statement=all on a throwaway 17.10
-- cluster, a recall with all three arms live runs the vector-arm SELECT, its
-- `SET LOCAL ivfflat.probes`, the lexical-arm SELECT and the graph re-filter
-- SELECT. Nothing else. So this role gets SELECT and nothing else — no INSERT,
-- no UPDATE, no DELETE, and no sequence privilege, because nextval is only
-- reachable through an INSERT this role cannot make. A policy that hides rows
-- from a role which can still DELETE them is not a floor.
--
-- WHY `ALTER DEFAULT PRIVILEGES` IS NOT OPTIONAL. The `GRANT ... ON ALL TABLES`
-- below is a snapshot: it covers the tables that exist when this runs and nothing
-- created afterwards. Every future migration and every one of those lazy runtime
-- tables would land ungranted, and recall against them would fail with a
-- permission error — on one customer, months later, exactly the failure shape the
-- paragraph above is about. The default-privileges rules close that for good,
-- because every table on this database is created by `entity` (it is the only
-- login role; the brain pool connects as it — server/brain_service.py:93-97).
--
-- WHAT THIS DOES NOT DO, stated plainly so nobody reads it as more than it is.
-- pg_hba on this cluster is `local | all | all | trust` (initdb --auth=trust,
-- never rewritten afterwards by any file in this repo). So creating this role
-- takes nothing away: any code path inside the entity container can still open
-- `postgresql://entity@/hostmem` and have superuser. This migration makes a
-- restricted lane EXIST and makes it correct; it does not yet make the
-- privileged lane unreachable. That step is blocked on retiring runtime DDL
-- first.
--
-- ─────────────────────────────────────────────────────────────────────────────
-- THIS FILE IS RE-RUN ON EVERY BOOT, AND IT IS NOT RUN BY THE MIGRATION LOOP.
--
-- entrypoint.sh applies it UNCONDITIONALLY, in the same position as the
-- unconditional `CREATE TABLE IF NOT EXISTS schema_migrations` that precedes the
-- loop, and records version 139 in the same breath so the loop skips it.
--
-- That is not tidiness, it is the only thing that makes the re-assertion below
-- mean anything. The loop reads schema_migrations and `continue`s on an applied
-- version (entrypoint.sh, the `if [ "$applied" = "1" ]` guard), so a numbered
-- migration runs ONCE PER HOST, EVER. A re-assert reached through the loop
-- executes on no host that already applied it — which is every host it was
-- written for. Measured on a throwaway cluster: drift the role to
-- `SUPERUSER BYPASSRLS REPLICATION`, boot again, and the loop printed nothing
-- but "relation schema_migrations already exists" while the role stayed
-- superuser.
--
-- And the restore case inverts it. Roles are CLUSTER-level; schema_migrations is
-- a TABLE. The host backup is `pg_dump -Fc -Z0 --no-owner --no-privileges` of
-- this one database — there is no
-- `pg_dumpall --globals-only` anywhere in the entity lane, so the dump carries
-- schema_migrations and carries NEITHER the role NOR the grants. Restore into a
-- fresh cluster and version 139 reads as applied while `host_recall` does not
-- exist: create_pool fails, get_recall_pool() returns None, and the Host's read
-- path silently runs on the superuser lane behind one boot-time warning.
-- Measured: `FATAL: role "host_recall" does not exist`.
--
-- Because it re-runs, every statement below RECONCILES rather than adds. The
-- privileges are reset before they are granted, so a host that applied an
-- earlier version of this file (which granted INSERT/UPDATE/DELETE and sequence
-- usage) converges down to SELECT instead of keeping what it was given.
--
-- ROLLBACK, if a policy is ever added and has to come off again: DISABLE ROW
-- LEVEL SECURITY **FIRST**, then DROP POLICY. The other order leaves a window
-- where row security is on with no policy, and Postgres's default in that state
-- is deny-all — measured at 0 rows for the restricted lane, i.e. the Host's
-- memory reads as empty rather than as an error. Disable first, drop second.
-- ─────────────────────────────────────────────────────────────────────────────

BEGIN;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'host_recall') THEN
        -- NOREPLICATION is load-bearing, not decoration: a replication
        -- connection streams the WAL and can reconstruct every row in the
        -- cluster, which bypasses row security completely. A read role that can
        -- open one has no floor under it at all. It is also the CREATE ROLE
        -- default — stated anyway, because the whole point of this role is that
        -- its attributes are the wall.
        CREATE ROLE host_recall
            LOGIN
            NOSUPERUSER
            NOBYPASSRLS
            NOCREATEDB
            NOCREATEROLE
            NOREPLICATION;
        RAISE NOTICE 'v139: role host_recall created';
    ELSE
        -- Re-assert. An operator, a restored dump, or a hand-fix could have left
        -- the role with attributes that quietly defeat the split; this runs on
        -- every boot, so the drift lasts at most until the next one.
        ALTER ROLE host_recall
            LOGIN
            NOSUPERUSER
            NOBYPASSRLS
            NOCREATEDB
            NOCREATEROLE
            NOREPLICATION;
    END IF;
END $$;

-- Role MEMBERSHIP survives an attribute re-assert and defeats the split on its
-- own: `GRANT entity TO host_recall` lets the restricted connection `SET ROLE
-- entity` and read every row, with rolsuper and rolbypassrls still reading
-- false in pg_roles. Measured on a throwaway cluster — after the drift boot,
-- `SET ROLE entity; SELECT current_user` returned `entity`.
--
-- Written as a loop over pg_auth_members rather than `REVOKE entity FROM
-- host_recall`, so it closes the class: membership in ANY role goes, including
-- one granted after this file was written. A bare REVOKE of a membership that
-- does not exist also emits a WARNING on every boot, which trains people to
-- ignore the boot log.
--
-- pg_read_all_data is deliberately NOT special-cased here. It is not a bypass:
-- verified on this image, a role holding it still sees only the rows a policy
-- allows. It would be revoked by this loop like any other membership, which is
-- correct — this role needs no grant it is not given below.
DO $$
DECLARE r record;
BEGIN
    FOR r IN
        SELECT g.rolname AS grantee_of
          FROM pg_auth_members m
          JOIN pg_roles g ON g.oid = m.roleid
         WHERE m.member = (SELECT oid FROM pg_roles WHERE rolname = 'host_recall')
    LOOP
        EXECUTE format('REVOKE %I FROM host_recall', r.grantee_of);
        RAISE NOTICE 'v139: revoked host_recall''s membership in %', r.grantee_of;
    END LOOP;
END $$;

-- No password, deliberately: the cluster is unix-socket-only
-- (`listen_addresses = ''`, entrypoint-postgres.sh write_pg_conf) and pg_hba is
-- trust, so this role authenticates exactly the way `entity` does today. What
-- separates them is privilege, not secrecy.

-- RESET, then grant. Everything from here down is a reconciliation to a known
-- state, because this file runs again on the next boot and on every boot after.
REVOKE ALL ON SCHEMA public FROM host_recall;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM host_recall;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM host_recall;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM host_recall;
ALTER DEFAULT PRIVILEGES FOR ROLE entity IN SCHEMA public
    REVOKE ALL ON TABLES FROM host_recall;
ALTER DEFAULT PRIVILEGES FOR ROLE entity IN SCHEMA public
    REVOKE ALL ON SEQUENCES FROM host_recall;

GRANT USAGE ON SCHEMA public TO host_recall;

-- SELECT on everything that exists now. No DDL, no ownership, no write: an
-- ordinary (non-owning) role is what RLS binds without needing FORCE ROW LEVEL
-- SECURITY on every table.
GRANT SELECT ON ALL TABLES IN SCHEMA public TO host_recall;

-- And the same for everything `entity` creates from here on — future migrations
-- and the lazy runtime tables both.
ALTER DEFAULT PRIVILEGES FOR ROLE entity IN SCHEMA public
    GRANT SELECT ON TABLES TO host_recall;

-- ...WITH ONE EXCEPTION, AND IT IS THE ONE THAT MATTERS.
--
-- "SELECT on everything" above is what makes RLS bindable without touching
-- every table, and it is right for memory. It is wrong for key material.
-- `kernel_secrets.secret` is plain BYTEA and it is not one key, it is the
-- host's whole key drawer: the entity's Ed25519 signing key
-- (server/applications/entity_signing.py), `vault_host_x25519` — the X25519
-- private key that opens every sealed credential the user has stored, provider
-- API keys, harness sign-ins, site databases, SSH keys (server/vault/identity.py)
-- — the app-token HMAC (server/applications/tokens.py) and the TOTP step-up
-- seed, enrolled marker and counter (server/network/nac_stepup.py). Read access
-- to that last one alone forges every step-up on the host.
-- v106_backup_user_key.sql already records the vault half, and it is the reason
-- the user's backup key is deliberately NOT sealed to the host key.
--
-- `nac_agent_delegations.token` is the second one: a plaintext bearer value
-- that marks an agent as REQUESTED on a server. Its own DDL comment calls it
-- soft, expiring and revocable, and the two readers
-- (server/network/nac_agents.py:417, 473, 583) use it for classification rather than
-- as a wall — so this is the lesser of the two. It is still a bearer token, and
-- a role that can read one can present it.
--
-- The point of giving recall its own role is that a query which reaches it
-- through the model can do no more than read memory. Reading the key that
-- decrypts every credential is more, so both are revoked here.
--
-- GUARDED, because both tables are created lazily by the first caller that
-- needs them and may not exist yet on a fresh host. The matching revokes at
-- their CREATE sites (tokens.py `_SECRET_SCHEMA`, nac_agents.py
-- `_DELEGATIONS_DDL`) are what cover that case — the ALTER DEFAULT PRIVILEGES
-- immediately above would otherwise hand host_recall SELECT the moment either
-- is finally created. Both halves are needed; neither is sufficient.
--
-- STRUCTURAL GAP, STATED SO IT IS NOT MISTAKEN FOR SOLVED: this is
-- "grant everything, revoke the exceptions", and the exception list is
-- maintained by hand. Nothing fails when the NEXT table with a plaintext
-- secret column is added. A tripwire that refuses an unlisted secret-shaped
-- column is the real answer and does not exist yet.
DO $$
DECLARE
    t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['kernel_secrets', 'nac_agent_delegations',
                             'owner_account', 'owner_sessions',
                             'owner_passkeys', 'owner_recovery_codes'] LOOP
        IF to_regclass('public.' || t) IS NOT NULL THEN
            EXECUTE format('REVOKE ALL ON public.%I FROM host_recall CASCADE', t);
            RAISE NOTICE 'v139: % is NOT readable by host_recall', t;
        ELSE
            RAISE NOTICE 'v139: % absent — its CREATE site revokes', t;
        END IF;
    END LOOP;
END $$;

COMMIT;
