-- v147: the last of server/ stops creating its own schema
--
-- Eight modules — the whole remaining `server/` allowlist — hand their schema
-- over to migrations/. Sixty-six runtime statements go; this file is what
-- replaces them, and it is fifteen (4 CREATE TABLE, 3 INDEX, 6 ALTER TABLE,
-- 1 backfill UPDATE, 1 REVOKE; the two DO blocks are guards, not replacements).
-- That ratio is the finding, so it is written down before the SQL:
--
--   server/routes/context.py        1 CREATE + 13 INDEX + 15 ALTER  -> NO SQL HERE
--   server/routes/compute.py        3 CREATE + 10 INDEX +  4 ALTER  -> NO SQL HERE
--   server/applications/registry.py 1 CREATE +  1 INDEX +  5 ALTER  -> 5 ALTERs below
--   server/vault/store.py           2 CREATE +  3 INDEX +  1 ALTER  -> 2 CREATEs +
--                                                                      3 INDEXes +
--                                                                      1 ALTER below
--   server/backup_key.py            2 CREATE +  1 INDEX             -> NO SQL HERE
--   server/applications/trust_store.py  1 CREATE + 1 INDEX          -> NO SQL HERE
--   server/vault/identity.py        1 CREATE                        -> the table below
--   server/applications/tokens.py   1 CREATE                        -> the table below
--
-- The three vault tables live IN this file — see the init-db.sql note below,
-- which is the reason they have to. This map is load-bearing: it is what
-- someone writing a later migration reads to decide whether a table is
-- already declared, and a wrong row is the same mistake in the other
-- direction. Measure it from the statements below; never carry it forward
-- from a draft.
--
-- HOW THAT WAS ESTABLISHED, because "no SQL here" is the claim that could do
-- the damage if it were wrong. init-db.sql plus all 142 shipped migrations were
-- applied under ON_ERROR_STOP=1 to a clean cseries-postgres, then projected
-- through the SAME survey query the eight live hosts were read with. What a brand-new host gets with no
-- runtime DDL at all was then diffed, column by column and index by index,
-- against what the live hosts actually carry. Every table above except
-- kernel_secrets came back FULLY DECLARED — all 29 runtime CREATE INDEX names
-- and 19 of the 25 runtime ADD COLUMNs already exist in the declared schema.
-- The runtime DDL was a defensive MIRROR, and copying a mirror into
-- migrations/ would put a second CREATE of a live table where it reads as
-- authoritative while being free to drift from the real owner.
--
-- So each is named with the file and line that already declares it:
--
--   user_context               init-db.sql:1164, and migrations/
--                              v090_user_context_create.sql:30 for the hosts
--                              whose pgdata predates it
--     ...its 15 runtime-ALTERed columns are spread across v049, v071, v078,
--        v084, v095, v096 and v135. Not mapped one-by-one here on purpose —
--        the projection above is what establishes they are all present, and a
--        hand-written mapping would be a second claim to keep true.
--   harness_usage              init-db.sql:1547
--   harness_usage_event        migrations/v114_harness_usage_event.sql:57
--   harness_run                migrations/v079_harness_run.sql:15
--     ...and idx_harness_run_commit  v128_harness_run_commit_index.sql:13
--   installed_applications     migrations/v030_app_runtime_tables.sql:55
--   application_signing_keys   migrations/v030_app_runtime_tables.sql:41
--   backup_user_key            migrations/v106_backup_user_key.sql:33
--   backup_key_platform_state  migrations/v106_backup_user_key.sql:70
--   vault_envelope             init-db.sql:1317
--   vault_audit                init-db.sql:1340
--   vault_identity_pubkey      init-db.sql:1334
--
-- Nothing below depends on a column's current TYPE or on the CONTENTS of a
-- table. An ALTER does depend on its table existing — that is the one way a
-- file like this can abort a boot under entrypoint.sh's ON_ERROR_STOP=1, and
-- every ALTER here either creates its own target first or is guarded. See the
-- init-db.sql note below, which is where that stopped being theoretical.
-- Idempotent and forward-only, per migrations/README.md.
--
-- ─────────────────────────────────────────────────────────────────────────────
-- init-db.sql IS NOT A MIGRATION, AND THE FIRST DRAFT OF THIS FILE TREATED IT
-- AS ONE. Read this before adding to the "already declared, write no SQL" list.
--
-- entrypoint.sh:314 runs init-db.sql ONLY when the schema probe says the
-- database is empty. It never re-runs on a host that already exists. The
-- migration loop at :405 is the opposite: it runs on every boot and applies
-- whatever that host has not applied. So "declared in init-db.sql" and
-- "declared in a migration" are NOT the same fact:
--
--   declared in a migration  -> every host will have it, or will get it
--   declared in init-db.sql  -> hosts provisioned BEFORE it landed there never
--                               got it, and nothing will ever give it to them
--
-- That is not hypothetical. vault_identity_pubkey is declared at
-- init-db.sql:1334 and in no migration, and it is ABSENT on two live
-- hosts (measured, both at schema_migrations 142). Its only
-- other creator was the runtime `_PUBKEY_SCHEMA` in server/vault/identity.py,
-- which this change deletes. Deleting it with nothing behind it would have
-- 500'd POST /api/backup/key/generate, /api/backup/key/register,
-- /api/vault/user-keys and /api/vault/seal-for-user on those two hosts — after
-- the passphrase check and the TOTP step-up had already been consumed.
--
-- The same reasoning applies to vault_envelope and vault_audit, and one of those
-- hosts is the evidence: it HAS them while lacking vault_identity_pubkey, which means
-- they did not come from init-db.sql on that host — they came from the runtime
-- `ensure_schema` in server/vault/store.py that this change also deletes. All
-- three are declared below. None of them has ever been ALTERed by a migration,
-- so init-db's CREATE plus this file is their complete shape.
--
-- harness_usage is in the same class (init-db.sql:1547, no migration) and is
-- deliberately NOT restated here. FOUR columns are added to it by migrations
-- that are already recorded-applied on every host and would not re-run —
-- entity_id and billing_ref (v080:19, :20), vendor_cost_microdollars (v089:11)
-- and phase_id (v112:30). Restating the bare init-db CREATE would hand a host
-- that lacked it a table four columns short, which is worse than the gap. It is
-- present on 8/8, so there is nothing to heal; if that ever changes it needs
-- its own migration carrying the full current shape, not a copy of init-db's.
-- ─────────────────────────────────────────────────────────────────────────────

BEGIN;

-- ─────────────────────────────────────────────────────────────────────────────
-- kernel_secrets — DECLARED NOWHERE until now, and it is the host's key drawer.
--
-- `secret` is plain BYTEA and it holds, under different ids: the entity's
-- Ed25519 signing key (server/applications/entity_signing.py),
-- `vault_host_x25519` — the X25519 private key that opens every sealed
-- credential the user has stored (server/vault/identity.py), the app-token HMAC
-- (server/applications/tokens.py), and the TOTP step-up seed, enrolled marker
-- and counter (server/network/nac_stepup.py).
--
-- Until now the only thing that ever created it was `_SECRET_SCHEMA` in
-- tokens.py, executed by whichever of those four callers reached it first. It
-- is live on all 8 hosts with ONE shape — same three columns, same types, same
-- nullability, same default, same single primary-key index — so there is no
-- drift for this migration to reconcile and it is a pure no-op on every
-- existing host.
--
-- The shape below is the LIVE shape, read from the hosts, not the code.
-- Unqualified, like every other CREATE in migrations/ — `public` is the only
-- schema on this database. The REVOKE below stays qualified because it mirrors
-- v139's, and because a REVOKE names an object rather than creating one.
CREATE TABLE IF NOT EXISTS kernel_secrets (
    id          TEXT PRIMARY KEY,
    secret      BYTEA NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- THE REVOKE BELONGS IN THIS FILE, IMMEDIATELY AFTER THE CREATE, AND MOVING THE
-- CREATE HERE IS EXACTLY WHY.
--
-- v139_host_recall_role.sql runs on EVERY boot and NOT through the migration
-- loop — entrypoint.sh:392-401 applies it unconditionally and records version
-- 139 in the same breath, so the loop at :405 skips it. Among the things it does
-- is `ALTER DEFAULT PRIVILEGES FOR ROLE entity ... GRANT SELECT ON TABLES TO
-- host_recall`, which fires the moment `entity` creates ANY table.
--
-- v139 also carries its own guarded revoke for this table — but it is guarded on
-- `to_regclass`, and on the one boot where THIS migration first applies, v139
-- has already run and found nothing. Sequence, on that boot:
--
--     v139 runs      -> kernel_secrets absent, its revoke skips
--     v139 runs      -> ALTER DEFAULT PRIVILEGES armed
--     v147 (here)    -> CREATE TABLE fires the default privilege:
--                       host_recall is handed SELECT on the key drawer
--     ...nothing takes it back until the NEXT boot
--
-- That window is the whole reason this statement is here rather than left to
-- v139. It is the same "both halves are needed; neither is sufficient" pairing
-- v139's own comment describes, with the CREATE site moved from Python to SQL.
--
-- Guarded on the role because a database restored without its cluster globals
-- has no host_recall (v139's own restore note), and `::regrole` RAISES on a role
-- that does not exist — which under ON_ERROR_STOP=1 is a host that will not
-- boot. CASCADE matches v139 and the runtime statement it mirrors.
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'host_recall') THEN
        REVOKE ALL ON public.kernel_secrets FROM host_recall CASCADE;
        RAISE NOTICE 'v147: kernel_secrets is NOT readable by host_recall';
    END IF;
END $$;

-- ─────────────────────────────────────────────────────────────────────────────
-- installed_applications — declared at v030_app_runtime_tables.sql:55, and that
-- file's own header says the Python helpers were left in place as idempotent
-- safety nets. Five columns were added to the Python SCHEMA_SQL afterwards and
-- never reached a .sql, so they exist on all 8 live hosts and on no fresh one.
-- Added here in the live column order, with the live types and defaults.
--
-- The comments are the ones the runtime block carried; they explain why each
-- column exists and are the only record of it.

-- GUARDED, and the guard is about blast radius rather than doubt. v030 is a
-- MIGRATION, so every host has installed_applications and this will always take
-- the ELSE branch. But an unguarded ALTER against a missing table is an ERROR,
-- and under entrypoint.sh's ON_ERROR_STOP=1 one ERROR anywhere in this file
-- rolls back the whole transaction — including the CREATEs above — and the host
-- does not boot at all. A WARNING that names the table degrades one feature
-- instead of taking the host down, and it is visible in the boot log rather
-- than silent. (Measured: applying an ALTER for a missing table exits rc=3.)
DO $$
BEGIN
IF to_regclass('public.installed_applications') IS NULL THEN
    RAISE WARNING 'v147: installed_applications is absent — the five app columns '
                  'and the authored_here backfill were SKIPPED. It is declared in '
                  'migrations/v030_app_runtime_tables.sql:55, so this host is in a '
                  'state that should not be reachable. Investigate before using '
                  'the app store.';
ELSE

-- The author's licence, carried from the (signature-covered) manifest at
-- install time so the rights an app was published under are readable on the
-- installing machine without re-opening the bundle.
    ALTER TABLE installed_applications
        ADD COLUMN IF NOT EXISTS app_license JSONB NOT NULL DEFAULT '{}'::jsonb;

-- Whether THIS machine is the app's author. NULL means unknown.
    ALTER TABLE installed_applications
        ADD COLUMN IF NOT EXISTS authored_here BOOLEAN;

-- The user's switch for a resident (always-on) process app: TRUE = keep it
-- running (start at boot, restart on crash), FALSE = the user turned it off,
-- NULL = never touched, so the manifest's runtime.lifecycle decides. Kernel-
-- owned on purpose — it is NOT in the app's own config store, so a resident
-- app cannot flip its own off switch. (resident lifecycle)
    ALTER TABLE installed_applications
        ADD COLUMN IF NOT EXISTS resident_enabled BOOLEAN;

-- The recipe: the app's source
-- origin (platform repo, packed commit/tree, upstream + its licence) and its
-- build (hashed lockfile + the hash the platform stamped), copied from the
-- signature-covered manifest at install like the licence above, so the listing,
-- the licence gates and the runtime pod read the installed ROW and never
-- re-open the bundle. '{}' = a row written before the recipe existed or an app
-- with no repo behind it; readers treat a missing key as absent.
    ALTER TABLE installed_applications
        ADD COLUMN IF NOT EXISTS app_source JSONB NOT NULL DEFAULT '{}'::jsonb;
    ALTER TABLE installed_applications
        ADD COLUMN IF NOT EXISTS app_build JSONB NOT NULL DEFAULT '{}'::jsonb;

-- THE BACKFILL MOVES WITH THE COLUMN, and it is load-bearing rather than
-- tidy-up. Without it every app an entity had ALREADY built in the Workbench
-- sits on `authored_here` NULL, and licensing.py:167-168 then puts the user's
-- own app into foreign_app_index — "published by someone else" — because that
-- reader tests the column directly and has no second signal. (The site-publish
-- gate does have one: routes/applications.py:2762-2775 accepts
-- provenance='custom' as sufficient. The two readers disagree, which is why
-- deleting this UPDATE would have been a quiet behaviour change rather than a
-- loud one.)
--
-- provenance='custom' IS the authorship signal, not a proxy for one: the
-- entity's own signing key is registered provenance_allowed=['custom']
-- (entity_signing.py:85-89) and it is the ONLY key that carries that value —
-- the seeded Privatae key is ['native','standard'] and the Community key is
-- ['community'] (trust_store.py).
-- A custom row therefore could only have been signed here. Idempotent: it can
-- only move NULL, never an answer.
--
-- ONCE AT BOOT IS ENOUGH, which is the claim that licenses moving it out of the
-- request path, so here is the check behind it. This can only ever fix rows
-- written BEFORE the column existed, because no live path can write a new one:
-- the sole caller of registry.register() is installer.py:646, which always
-- passes an explicit `authored_here=(source == "self")` — a bool, never NULL.
-- registry.boot_sync() (registry.py:384) is the only other caller of register()
-- and it HAS NO CALLERS ITSELF — established by a repo-wide search, not by any
-- comment; nothing in registry.py states it, and its docstring at :20-23 reads
-- the other way. Its rows would be native/standard provenance anyway, which
-- this statement does not match.
    UPDATE installed_applications
       SET authored_here = TRUE
     WHERE authored_here IS NULL AND provenance = 'custom';
END IF;
END $$;
-- ─────────────────────────────────────────────────────────────────────────────
-- THE THREE VAULT TABLES — restated from init-db.sql because it is not a
-- migration (see the header). Shapes are init-db.sql:1317, :1334 and :1340
-- verbatim; no migration has ever ALTERed any of them, so these CREATEs plus
-- the one ADD COLUMN below are their complete declaration. Every statement is
-- IF NOT EXISTS, so on the hosts that already carry them this is a no-op.

-- Recipient public keys for non-local identities (users / platform). PUBLIC
-- keys only — the matching private keys live in those identities' own space.
-- ABSENT on some long-lived hosts; this is the statement that fixes it.
CREATE TABLE IF NOT EXISTS vault_identity_pubkey (
  identity    TEXT PRIMARY KEY,
  pubkey      BYTEA NOT NULL,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS vault_envelope (
  id             UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_identity TEXT         NOT NULL,
  logical_name   TEXT         NOT NULL,
  cred_type      TEXT         NOT NULL,
  envelope       JSONB        NOT NULL,
  kek_version    INT          NOT NULL DEFAULT 1,
  created_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
  superseded_at  TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_vault_envelope_active
  ON vault_envelope (owner_identity, logical_name) WHERE superseded_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_vault_envelope_owner
  ON vault_envelope (owner_identity) WHERE superseded_at IS NULL;

CREATE TABLE IF NOT EXISTS vault_audit (
  id             BIGSERIAL    PRIMARY KEY,
  at             TIMESTAMPTZ  NOT NULL DEFAULT now(),
  actor          TEXT         NOT NULL,
  op             TEXT         NOT NULL,
  owner_identity TEXT,
  logical_name   TEXT,
  detail         JSONB
);
CREATE INDEX IF NOT EXISTS idx_vault_audit_name
  ON vault_audit (owner_identity, logical_name, at DESC);

-- The lifecycle column init-db.sql predates. A DEACTIVATED credential
-- (deactivated_at set, still active i.e. superseded_at NULL) stays sealed but
-- resolves as absent — a temporary off-switch,
-- distinct from revoke (supersede, permanent, re-issue to come back). Live on
-- all 8 hosts, on no fresh one. Its target is guaranteed by the CREATE above,
-- which is why this one needs no guard.
ALTER TABLE vault_envelope
    ADD COLUMN IF NOT EXISTS deactivated_at TIMESTAMPTZ;

COMMIT;
