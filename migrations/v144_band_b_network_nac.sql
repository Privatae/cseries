-- v144: the network/NAC cluster stops creating its own schema
--
-- Four modules — server/network/nac_agents.py, nac_mgmt.py, topology.py and
-- peer_discovery.py — carried 42 DDL statements between them, run on the
-- heartbeat path and on nearly every Network-app request. Schema belongs in the
-- declared files, applied once at boot by entrypoint.sh, so the running kernel
-- needs no DDL rights at all. Nothing about any of these features changes: every
-- read and every write stays exactly where it was, only the self-bootstrapping
-- is gone.
--
-- 42 STATEMENTS GO, 7 ARRIVE HERE. That ratio is the whole point of this change and
-- it is worth stating plainly, because the obvious way to do this work is wrong.
-- Eight of the ten tables those modules created are ALREADY declared, in
-- init-db.sql or in a shipped migration, and the runtime CREATE is a defensive
-- mirror of a declaration that already exists. Copying such a CREATE into a new
-- migration does not fix drift, it manufactures it: two declarations of one
-- table, disagreeing the first time either is edited. So the eight get no SQL
-- here at all, only deletion, and each one's real declaration is named below so
-- the next reader does not have to re-derive it.
--
--     nac_agent_activity          v061_nac_agents.sql:13
--     nac_agent_delegations       v064_nac_agent_delegations.sql:10
--     nac_agent_contain           v065_nac_agent_contain.sql:11
--     nac_mgmt_grants             v060_nac_mgmt.sql:17
--     nac_mgmt_audit              v060_nac_mgmt.sql:38
--     nac_bastion_quarantine      v069_nac_bastion_quarantine.sql:25
--     network_bastion_peer        v132_bastion_peers.sql:69
--     network_bastion_heartbeat   v056_network_topology.sql:20 + v057:16
--
-- THAT LAST ONE IS THE TRAP, and it is why this was checked by APPLYING the
-- declared schema rather than by grepping for table names. v056 creates
-- `network_gateway_heartbeat`; v057_gateway_to_bastion_rename.sql:16 renames it
-- to `network_bastion_heartbeat`. No search for the current name finds the
-- statement that declares it — scripts/undeclared_tables_baseline.txt (frozen
-- by hand) lists this table as undeclared for exactly that reason,
-- and it is wrong. A name-grep cannot see a table declared through a RENAME;
-- running the SQL can. Everything below rests on the measured difference between
-- init-db.sql plus all 137 shipped migrations on an empty database, and the
-- shapes the 8 live hosts actually carry.
--
-- These statements are written against that LIVE shape, not against what the
-- feature files would produce today. Where the two disagree, LIVE WINS and this
-- file only ever ADDS what is missing. entrypoint.sh:416 runs every migration
-- with ON_ERROR_STOP=1, so one statement that assumes a column's current type,
-- or that depends on the rows already in a table, is a host that does not boot.
--
--
-- ── nac_agent_audit — declared NOWHERE, and missing on 3 of 8 hosts ──
--
-- The append-only record of every agent-governance disposition (sanction,
-- acknowledge, contain, revoke_delegation): who did what to which agent, when.
-- It has never had a migration. It exists on hosts 102, 122, 123, 128 and 3
-- because somebody there took one of those actions and the lazy CREATE fired;
-- it is absent on 121, 130 and 55 because nobody has. Deleting the Python
-- without this CREATE would take the audit trail away from those three
-- permanently — the disposition would still happen, and the record of it would
-- silently fail to be written.
--
--
-- ── nac_bastion_suspend — declared NOWHERE, and missing on the same 3 ──
--
-- A suspend is a TIGHTEN with no step-up: the gate denies a server even when its
-- signed grants are valid. The table is empty on all 8 hosts, which is what a
-- table for a rarely-used lever looks like and not evidence it is unused. Same
-- shape of loss as above: without this, the three hosts that have never
-- suspended a box lose the ability to, and the failure lands on the gate path.
--
--
-- ── nac_agent_activity: family, kind, ppid — the 4/8 split ──
--
-- v061 declares this table and four later migrations add five columns to it
-- (v062 llm_endpoints, v066 notified_at, v067 acknowledged_at, v129 origin and
-- evidence). Three columns never got a migration and arrived only through the
-- runtime ALTERs in nac_agents.ensure_table: family, kind and ppid. Hosts 102,
-- 122, 128 and 3 have them; 121, 123, 130 and 55 do not, and that is precisely
-- the divergence this change exists to end — the schema a host ends up with
-- depending on which code paths happened to run there.
--
-- They are not cosmetic. `kind` is what separates an agent from the Claude
-- desktop app (one open app once arrived as nine agents and raised nine alerts
-- with nine kill buttons); `family` is what groups a lineage into one card and
-- one alert; `ppid` is what makes that lineage derivable. ingest() writes all
-- three on every heartbeat and list_observations() reads them.
--
-- WHICH FOUR HOSTS, AND WHY THOSE FOUR. The survey's row counts answer it: the
-- four hosts that have the columns are the four with a non-empty
-- network_bastion_heartbeat (102=1, 122=1, 128=1, 3=28 rows), and the four
-- without them have none (121, 123, 130, 55 = 0). ensure_table() runs on the
-- heartbeat path, so the ALTERs fired exactly where a bastion has checked in and
-- nowhere else. Nothing has been failing on the other four — nothing has run
-- there yet. What this migration prevents is the first bastion to enrol on one
-- of them meeting a table with no kind, no family and no ppid once the runtime
-- ALTERs are gone, which would take the INSERT in ingest() down its per-row
-- `except` and lose every observation silently.
--
--
-- ── network_bastion_heartbeat: models — the same four hosts ──
--
-- The models a bastion reports it serves over the mesh, read by
-- mastercode.model_backends for the local-lane default match. Twenty-one columns
-- on this table arrived through runtime ALTERs and twenty of them have a migration
-- behind them (v063, v102, v104, v118, v119, v129, v132, v133, v136). `models`
-- is the one that does not, and it splits the fleet the same 4/4 way and for the
-- same reason: the four hosts with a heartbeat row ran the ALTER, the four
-- without one never reached it.
--
-- NOT NULL DEFAULT '[]' is safe on a populated table: PostgreSQL 11+ stores the
-- default in the catalog and rewrites nothing, and the value is a constant, so
-- this depends on no existing row. It is an ADD, never a SET NOT NULL on a
-- column that already exists.
--
--
-- ── the guards, and what they are for ──
--
-- The four ALTERs depend on two tables this file does not declare. Both are
-- present on all 8 hosts today, so an unguarded statement could not bite now —
-- but deleting ensure_table() removes the backstop that has been closing that
-- hole, and an ALTER against a missing table aborts the whole migration under
-- ON_ERROR_STOP=1. The guard is here so it cannot bite later either. Same
-- pattern as v142.
--
--
-- ── ONE THING THAT IS DELIBERATELY NOT DELETED FROM THE PYTHON ──
--
-- nac_agents.py's _DELEGATIONS_DDL is two statements: a CREATE TABLE (which
-- v064 already owns, and which goes) and a guarded REVOKE ALL ON
-- nac_agent_delegations FROM host_recall (which STAYS). That revoke is the other
-- half of v139_host_recall_role.sql's exception list, and v139's own comment
-- says why both halves are needed: `ALTER DEFAULT PRIVILEGES ... GRANT SELECT ON
-- TABLES TO host_recall` hands the read out the moment the table is created, so
-- a host where the table arrived AFTER v139 ran needs a revoke at the runtime
-- site. `nac_agent_delegations.token` is a plaintext bearer value.
-- scripts/test_role_split_rls.sh:273-317 asserts that constant exists, matches
-- that revoke, and is still executed. A to_regclass guard is added around it,
-- because without the CREATE beside it the table is no longer guaranteed to
-- exist when the revoke runs. No privilege statement belongs in this file: v139
-- re-runs on every boot and owns that lane.
--
-- Idempotent and forward-only, per migrations/README.md.

BEGIN;

-- ── nac_agent_audit ──────────────────────────────────────────────────
-- Was server/network/nac_agents.py _AGENT_AUDIT_DDL. Reproduced exactly as the
-- five hosts that have it carry it, index included.

CREATE TABLE IF NOT EXISTS nac_agent_audit (
    id           BIGSERIAL PRIMARY KEY,
    entity_id    INTEGER NOT NULL,
    disposition  TEXT NOT NULL,          -- sanction | acknowledge | contain | revoke_delegation
    token_id     INTEGER,
    pid          INTEGER,
    target       TEXT,
    detail       JSONB,
    actor        TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS nac_agent_audit_entity
    ON nac_agent_audit (entity_id, created_at DESC);

-- ── nac_bastion_suspend ──────────────────────────────────────────────
-- Was server/network/nac_mgmt.py _SUSPEND_DDL.

CREATE TABLE IF NOT EXISTS nac_bastion_suspend (
    server        TEXT PRIMARY KEY,
    suspended_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    suspended_by  TEXT
);

-- ── the four missing columns ─────────────────────────────────────────
-- Guarded: these are the only statements here that touch a table this file does
-- not declare. See "the guards" above.

DO $$
BEGIN
    IF to_regclass('public.nac_agent_activity') IS NULL THEN
        RAISE NOTICE 'v144: nac_agent_activity absent — v061 declares it, so this '
                     'database predates that migration; nothing to add';
    ELSE
        ALTER TABLE nac_agent_activity ADD COLUMN IF NOT EXISTS kind   TEXT;
        ALTER TABLE nac_agent_activity ADD COLUMN IF NOT EXISTS ppid   INTEGER;
        ALTER TABLE nac_agent_activity ADD COLUMN IF NOT EXISTS family TEXT;
    END IF;

    IF to_regclass('public.network_bastion_heartbeat') IS NULL THEN
        RAISE NOTICE 'v144: network_bastion_heartbeat absent — v056 creates it as '
                     'network_gateway_heartbeat and v057 renames it, so this database '
                     'predates one of those; nothing to add';
    ELSE
        ALTER TABLE network_bastion_heartbeat
            ADD COLUMN IF NOT EXISTS models JSONB NOT NULL DEFAULT '[]';
    END IF;
END $$;

COMMIT;
