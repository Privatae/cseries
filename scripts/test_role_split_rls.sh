#!/usr/bin/env bash
# test_role_split_rls.sh — prove the host_recall role split on a THROWAWAY cluster.
#
# WHY THIS EXISTS. v139 creates `host_recall` and grants it DML, and the claim
# attached to it is a security claim: that an RLS policy written against this role
# actually HIDES rows, and that the same policy is invisible to `entity`. A claim
# like that is worth exactly as much as the run that demonstrates it, so this is
# the run. It is also the negative control the design asks for — with no policy
# installed the two roles must return byte-identical row sets, because that is the
# property that makes v139 safe to ship ahead of any policy.
#
# NEVER POINT THIS AT A LIVE HOST. It creates roles, enables row security and
# writes rows. It takes no DSN and no host argument on purpose: it starts its own
# container, does the work there, and removes it. The eight live customer hosts
# are not reachable from anything in this file.
#
# It is NOT part of scripts/pre_merge_gates.sh — it needs docker and the
# cseries-postgres image, so it is not container-free. Run it by hand:
#
#     scripts/test_role_split_rls.sh
#     PG_IMAGE=cseries-postgres:smoke scripts/test_role_split_rls.sh
#
# Exit 0 = every assertion held.
set -uo pipefail

REPO="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
IMAGE="${PG_IMAGE:-cseries-postgres:smoke}"
CNAME="${RLS_TEST_CONTAINER:-role-split-proof-$$}"
MIGRATION="$REPO/migrations/v139_host_recall_role.sql"

red()   { printf '\033[31m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }
dim()   { printf '\033[2m%s\033[0m\n' "$*"; }

failed=0
check() {  # check <description> <expected> <actual>
    if [[ "$2" == "$3" ]]; then
        green "OK    $1"
        dim   "      got: $3"
    else
        red   "FAIL  $1"
        red   "      expected: $2"
        red   "      got:      $3"
        failed=$((failed + 1))
    fi
}

cleanup() { docker rm -f "$CNAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT

[[ -f "$MIGRATION" ]] || { red "no $MIGRATION"; exit 2; }
[[ -f "$REPO/init-db.sql" ]] || { red "no $REPO/init-db.sql"; exit 2; }

# ── stand the cluster up ────────────────────────────────────────────────────
echo "role-split proof — image $IMAGE, throwaway container $CNAME"
echo
docker rm -f "$CNAME" >/dev/null 2>&1 || true
docker run -d --name "$CNAME" "$IMAGE" >/dev/null || { red "could not start $IMAGE"; exit 2; }

for _ in $(seq 1 60); do
    docker exec "$CNAME" pg_isready -h /var/run/postgresql -U entity -d hostmem -q 2>/dev/null && break
    sleep 1
done
docker exec "$CNAME" pg_isready -h /var/run/postgresql -U entity -d hostmem -q 2>/dev/null \
    || { red "cluster never came up"; docker logs "$CNAME" 2>&1 | tail -20; exit 2; }

# psql as <role>, one statement, tuples only.
as() { docker exec "$CNAME" psql -h /var/run/postgresql -U "$1" -d hostmem -Atq -c "$2" 2>&1; }

docker cp "$REPO/init-db.sql" "$CNAME:/tmp/init-db.sql" >/dev/null
docker cp "$MIGRATION"        "$CNAME:/tmp/v139.sql"    >/dev/null
docker exec "$CNAME" psql -h /var/run/postgresql -U entity -d hostmem \
    --single-transaction -f /tmp/init-db.sql >/dev/null 2>&1
check "the real init-db.sql loaded" "t" \
      "$(as entity "SELECT to_regclass('public.entity_memory') IS NOT NULL")"

# The state this migration is written against, asserted rather than assumed:
# one login role, superuser, carrying the explicit RLS bypass.
check "before v139: entity is the only login role, superuser, rolbypassrls" \
      "entity|true|true" \
      "$(as entity "SELECT string_agg(rolname||'|'||rolsuper::text||'|'||rolbypassrls::text, ',' ORDER BY rolname) FROM pg_roles WHERE rolcanlogin")"
check "before v139: no table on this database has row security enabled" "0" \
      "$(as entity "SELECT count(*) FROM pg_class WHERE relrowsecurity")"

# ── v139, twice ─────────────────────────────────────────────────────────────
docker exec "$CNAME" psql -h /var/run/postgresql -U entity -d hostmem \
    -v ON_ERROR_STOP=1 -f /tmp/v139.sql >/dev/null 2>&1
check "v139 applies" "0" "$?"
docker exec "$CNAME" psql -h /var/run/postgresql -U entity -d hostmem \
    -v ON_ERROR_STOP=1 -f /tmp/v139.sql >/dev/null 2>&1
# It has to be re-runnable because entrypoint.sh applies it on EVERY boot, from
# the unconditional position before the migration loop — never through the loop,
# which is once-per-host-ever. PHASE D and PHASE E below drive the entrypoint's
# real text and prove that placement rather than trusting this line.
check "v139 re-runs cleanly (the entrypoint applies it at every boot)" "0" "$?"

check "host_recall: not super, no bypassrls, no createdb, no createrole, no replication, can log in" \
      "false|false|false|false|false|true" \
      "$(as entity "SELECT rolsuper||'|'||rolbypassrls||'|'||rolcreatedb||'|'||rolcreaterole||'|'||rolreplication||'|'||rolcanlogin FROM pg_roles WHERE rolname='host_recall'")"

# ── the grants do what they have to, and NOTHING they do not ────────────────
check "host_recall can read entity_memory" "0" "$(as host_recall "SELECT count(*) FROM entity_memory")"
check "host_recall CANNOT create a table (it is not secretly privileged)" \
      "ERROR:  permission denied for schema public" \
      "$(as host_recall "CREATE TABLE must_not_exist (id int)" | head -1)"
check "host_recall CANNOT hand itself BYPASSRLS" \
      "ERROR:  permission denied to alter role" \
      "$(as host_recall "ALTER ROLE host_recall BYPASSRLS" | head -1)"
check "host_recall CANNOT SET ROLE entity" \
      'ERROR:  permission denied to set role "entity"' \
      "$(as host_recall "SET ROLE entity" | head -1)"

# READ-ONLY, asserted per verb. `_read_pool` has exactly one consumer
# (memory_retrieval.recall_memories) and every statement it issues is a SELECT —
# measured with log_statement=all: the vector-arm SELECT, its SET LOCAL
# ivfflat.probes, the lexical-arm SELECT, the graph re-filter SELECT. A policy
# that hides rows from a role which can still DELETE them is not a floor.
check "host_recall CANNOT DELETE FROM entity_memory" \
      "ERROR:  permission denied for table entity_memory" \
      "$(as host_recall "DELETE FROM entity_memory WHERE true" | head -1)"
check "host_recall CANNOT INSERT INTO entity_memory" \
      "ERROR:  permission denied for table entity_memory" \
      "$(as host_recall "INSERT INTO entity_memory (entity_id, content, memory_type) VALUES (1,'x','cited_fact')" | head -1)"
check "host_recall CANNOT UPDATE entity_memory" \
      "ERROR:  permission denied for table entity_memory" \
      "$(as host_recall "UPDATE entity_memory SET content='x'" | head -1)"
check "host_recall CANNOT TRUNCATE entity_memory" \
      "ERROR:  permission denied for table entity_memory" \
      "$(as host_recall "TRUNCATE entity_memory" | head -1)"
check "host_recall holds SELECT and only SELECT on entity_memory" "SELECT" \
      "$(as entity "SELECT COALESCE(string_agg(DISTINCT privilege_type, ',' ORDER BY privilege_type),'(none)')
                      FROM information_schema.role_table_grants
                     WHERE grantee='host_recall' AND table_name='entity_memory'")"

# The default-privileges half. Without it every future migration and every one of
# the lazy runtime CREATE TABLE sites lands ungranted and recall against it
# fails on one customer, months later.
as entity "CREATE TABLE lazy_runtime_table (id SERIAL PRIMARY KEY, note TEXT)" >/dev/null
as entity "INSERT INTO lazy_runtime_table (note) VALUES ('x')" >/dev/null
check "a table entity creates AFTER v139 is readable with no explicit grant" "1" \
      "$(as host_recall "SELECT count(*) FROM lazy_runtime_table")"
check "...and the default privilege is SELECT only there too" \
      "ERROR:  permission denied for table lazy_runtime_table" \
      "$(as host_recall "INSERT INTO lazy_runtime_table (note) VALUES ('y')" | head -1)"
check "...and no sequence privilege came with it (nextval is only reachable via INSERT)" \
      "ERROR:  permission denied for sequence lazy_runtime_table_id_seq" \
      "$(as host_recall "SELECT nextval('lazy_runtime_table_id_seq')" | head -1)"

# ── M-2b: KEY MATERIAL IS THE EXCEPTION TO "SELECT ON EVERYTHING" ───────────
# The blanket grant just proven is right for memory and wrong for secrets.
# kernel_secrets.secret is plain BYTEA and holds the entity's Ed25519 signing
# key (server/applications/entity_signing.py) and vault_host_x25519, the host's
# X25519 private key (server/vault/identity.py) — the key that opens every
# sealed credential the user has: provider API keys, harness sign-ins, SSH keys.
# A recall connection reachable through the model must not be able to read it.
#
# BOTH ORDERINGS ARE TESTED, because only one of them is covered by the
# migration. A table that already existed when v139 ran is revoked BY v139; a
# table created afterwards is caught by ALTER DEFAULT PRIVILEGES and has to be
# revoked at its CREATE site instead (tokens.py, _SECRET_SCHEMA). Getting only
# the first one right leaves every NEW host exposed, which is the dangerous half.
# THE REAL STRING, EXTRACTED FROM tokens.py — not a hand copy. A copy here meant
# reverting the REVOKE in tokens.py left this proof green, so the half of the fix
# that covers every NEW host was bound to nothing.
SECRET_SCHEMA=$(python3 - "$REPO" <<'PY'
import ast, pathlib, sys
src = pathlib.Path(sys.argv[1], "server/applications/tokens.py").read_text()
for n in ast.parse(src).body:
    if isinstance(n, ast.Assign) and any(
            getattr(t, "id", "") == "_SECRET_SCHEMA" for t in n.targets):
        print(n.value.value)
        break
else:
    raise SystemExit("tokens.py no longer defines _SECRET_SCHEMA")
PY
)
# EVERY RUN ASSERTS rc=0. Without this the xmin check below is satisfied by a
# string that ERRORS on re-run — it never gets far enough to write the catalog,
# so "xmin unchanged" reads OK while production logs the failure forever.
SS_RC=0
run_secret_schema () {
    docker exec -i "$CNAME" psql -h /var/run/postgresql -U entity -d hostmem -q \
        -v ON_ERROR_STOP=1 <<<"$SECRET_SCHEMA" >/dev/null 2>&1
    local rc=$?
    [[ $rc -eq 0 ]] || SS_RC=$rc
}
check "tokens.py still defines _SECRET_SCHEMA and it revokes from host_recall" "yes" \
      "$(grep -qE 'REVOKE ALL ON (public\.)?kernel_secrets FROM host_recall' <<<"$SECRET_SCHEMA" && echo yes || echo no)"
# A constant that is never EXECUTED is the one way the extraction above fails
# open: the string parses, the grep passes, and nothing runs it at runtime.
check "...and tokens.py actually EXECUTES it" "yes" \
      "$(grep -q 'conn.execute(_SECRET_SCHEMA)' "$REPO/server/applications/tokens.py" && echo yes || echo no)"

as entity "CREATE TABLE kernel_secrets (id TEXT PRIMARY KEY, secret BYTEA NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now())" >/dev/null
as entity "INSERT INTO kernel_secrets (id, secret) VALUES ('vault_host_x25519', '\\x00')" >/dev/null
check "a kernel_secrets created AFTER v139 is readable until the revoke runs (the hazard)" "1" \
      "$(as host_recall "SELECT count(*) FROM kernel_secrets")"
run_secret_schema
check "tokens.py's revoke closes it: host_recall CANNOT read kernel_secrets" \
      "ERROR:  permission denied for table kernel_secrets" \
      "$(as host_recall "SELECT secret FROM kernel_secrets" | head -1)"
check "...and the host itself still can, so nothing is broken by the revoke" "1" \
      "$(as entity "SELECT count(*) FROM kernel_secrets")"

# IDEMPOTENT, because this string runs UNCACHED on hot paths: get_secret() is
# called by _require_own_token on every app helper request (applications/proxy.py),
# by every host tool call, every 2FA verify and every vault open. A REVOKE that
# changes nothing still rewrites the table's pg_class row, and two overlapping
# sessions then raise "tuple concurrently updated". Measured on this image with
# the bare REVOKE: xmin 741->742->743 on three no-op runs, and 100/320 failures
# at 8x40 concurrency. With the has_table_privilege guard: 0/320.
XMIN_A=$(as entity "SELECT xmin FROM pg_class WHERE relname='kernel_secrets'")
run_secret_schema; run_secret_schema
check "re-running _SECRET_SCHEMA does NOT rewrite pg_class (no catalog churn on a hot path)" \
      "$XMIN_A" \
      "$(as entity "SELECT xmin FROM pg_class WHERE relname='kernel_secrets'")"

# ...and it RE-ARMS: a boot re-grants, and the next call must undo that.
as entity "GRANT SELECT ON kernel_secrets TO host_recall" >/dev/null
check "a re-grant makes it readable again (so the next check is not vacuous)" "t" \
      "$(as entity "SELECT has_table_privilege('host_recall','public.kernel_secrets','SELECT')")"
run_secret_schema
check "...and the next _SECRET_SCHEMA call revokes it again" "f" \
      "$(as entity "SELECT has_table_privilege('host_recall','public.kernel_secrets','SELECT')")"

# THE GUARD ASKS THE ACL, NOT EFFECTIVE PRIVILEGE, and these are the two states
# where those disagree in the dangerous direction. has_table_privilege(...,
# 'SELECT') answers FALSE for both, so a guard built on it would skip while the
# grant stands — weaker than no guard at all.
as entity "REVOKE ALL ON kernel_secrets FROM host_recall" >/dev/null
as entity "GRANT SELECT (secret) ON kernel_secrets TO host_recall" >/dev/null
check "a COLUMN-level grant is readable (the state a SELECT-privilege guard misses)" "00" \
      "$(as host_recall "SELECT encode(secret,'hex') FROM kernel_secrets")"
run_secret_schema
check "...and the ACL guard still revokes it" \
      "ERROR:  permission denied for table kernel_secrets" \
      "$(as host_recall "SELECT secret FROM kernel_secrets" | head -1)"

as entity "GRANT UPDATE, DELETE, TRUNCATE ON kernel_secrets TO host_recall" >/dev/null
check "a non-SELECT grant lets recall DESTROY the key drawer (guard-on-SELECT misses it)" \
      "true|true|true" \
      "$(as entity "SELECT has_table_privilege('host_recall','public.kernel_secrets','UPDATE')||'|'||has_table_privilege('host_recall','public.kernel_secrets','DELETE')||'|'||has_table_privilege('host_recall','public.kernel_secrets','TRUNCATE')")"
run_secret_schema
check "...and the ACL guard revokes that too" "false|false|false" \
      "$(as entity "SELECT has_table_privilege('host_recall','public.kernel_secrets','UPDATE')||'|'||has_table_privilege('host_recall','public.kernel_secrets','DELETE')||'|'||has_table_privilege('host_recall','public.kernel_secrets','TRUNCATE')")"

# MEMBERSHIP is the opposite direction: effective privilege says TRUE, but a
# REVOKE ... FROM host_recall cannot remove it, so a guard built on
# has_table_privilege fires on EVERY call and never converges — the catalog
# churn this whole commit exists to remove, back again. The ACL guard skips it.
as entity "GRANT entity TO host_recall" >/dev/null
XMIN_M=$(as entity "SELECT xmin FROM pg_class WHERE relname='kernel_secrets'")
run_secret_schema; run_secret_schema
check "a membership-derived privilege does NOT make the guard churn" \
      "$XMIN_M" \
      "$(as entity "SELECT xmin FROM pg_class WHERE relname='kernel_secrets'")"
as entity "REVOKE entity FROM host_recall" >/dev/null

check "every _SECRET_SCHEMA run above exited 0 (a string that errors would fake the xmin checks)" \
      "0" "$SS_RC"
# Now the migration's own half: re-running v139 must not hand the grant back.
docker exec -i "$CNAME" psql -h /var/run/postgresql -U entity -d hostmem -q -v ON_ERROR_STOP=1 \
    < "$MIGRATION" >/dev/null 2>&1
check "v139 re-run does NOT re-grant kernel_secrets (its own revoke holds)" \
      "ERROR:  permission denied for table kernel_secrets" \
      "$(as host_recall "SELECT secret FROM kernel_secrets" | head -1)"

# ── the SECOND plaintext-bearer table, bound the same way ───────────────────
# nac_agent_delegations.token marks an agent REQUESTED on a server. Lesser than
# kernel_secrets — its readers classify rather than gate — but a role that can
# read a bearer value can present it. Extracted from source, not hand-copied,
# for the same reason as _SECRET_SCHEMA above.
DELEGATIONS_DDL=$(python3 - "$REPO" <<'PY'
import ast, pathlib, sys
src = pathlib.Path(sys.argv[1], "server/network/nac_agents.py").read_text()
for n in ast.parse(src).body:
    if isinstance(n, ast.Assign) and any(
            getattr(t, "id", "") == "_DELEGATIONS_DDL" for t in n.targets):
        print(n.value.value)
        break
else:
    raise SystemExit("nac_agents.py no longer defines _DELEGATIONS_DDL")
PY
)
check "nac_agents.py's _DELEGATIONS_DDL revokes from host_recall" "yes" \
      "$(grep -qE 'REVOKE ALL ON (public\.)?nac_agent_delegations FROM host_recall' <<<"$DELEGATIONS_DDL" && echo yes || echo no)"
check "...and nac_agents.py actually EXECUTES it" "yes" \
      "$(grep -q 'execute(_DELEGATIONS_DDL)' "$REPO/server/network/nac_agents.py" && echo yes || echo no)"

# --single-transaction MATCHES PRODUCTION. asyncpg runs a multi-statement string
# as ONE implicit transaction (measured: a failure anywhere rolls the CREATE and
# the REVOKE back together); psql without this flag commits each statement
# separately, so the proof would accept a string that half-applies where the
# pods would not. And rc is RECORDED: without it a string that errors on every
# re-run satisfies the xmin check below by never getting far enough to write.
DD_RC=0
run_delegations_ddl () {
    docker exec -i "$CNAME" psql -h /var/run/postgresql -U entity -d hostmem -q \
        -v ON_ERROR_STOP=1 --single-transaction <<<"$DELEGATIONS_DDL" >/dev/null 2>&1
    local rc=$?
    [[ $rc -eq 0 ]] || DD_RC=$rc
}

as entity "DROP TABLE IF EXISTS nac_agent_delegations" >/dev/null
as entity "CREATE TABLE nac_agent_delegations (id BIGSERIAL PRIMARY KEY, token TEXT NOT NULL UNIQUE, server TEXT NOT NULL)" >/dev/null
check "...and a delegations table created after v139 is readable until it runs (the hazard)" "0" \
      "$(as host_recall "SELECT count(*) FROM nac_agent_delegations")"
run_delegations_ddl
check "...then host_recall CANNOT read the delegation tokens" \
      "ERROR:  permission denied for table nac_agent_delegations" \
      "$(as host_recall "SELECT token FROM nac_agent_delegations" | head -1)"
XMIN_D=$(as entity "SELECT xmin FROM pg_class WHERE relname='nac_agent_delegations'")
run_delegations_ddl; run_delegations_ddl
check "...and re-running it does NOT rewrite pg_class either" \
      "$XMIN_D" \
      "$(as entity "SELECT xmin FROM pg_class WHERE relname='nac_agent_delegations'")"
check "every _DELEGATIONS_DDL run exited 0 (a string that errors would fake the xmin check)" \
      "0" "$DD_RC"

# THE MIGRATION'S OWN HALF FOR THIS TABLE, which nothing asserted until now.
# v139's exception list is an ARRAY; trimming it back to just kernel_secrets
# left the whole proof green, so every restore-boot would quietly re-grant
# delegation tokens to host_recall and no gate would say a word. The table has
# to EXIST and be GRANTED when v139 runs, or the check is vacuous — the earlier
# v139 re-run happens before this table is created.
as entity "GRANT SELECT ON nac_agent_delegations TO host_recall" >/dev/null
check "a re-grant makes delegation tokens readable again (so the next check is not vacuous)" "0" \
      "$(as host_recall "SELECT count(*) FROM nac_agent_delegations")"
docker exec -i "$CNAME" psql -h /var/run/postgresql -U entity -d hostmem -q -v ON_ERROR_STOP=1 \
    < "$MIGRATION" >/dev/null 2>&1
check "v139 itself revokes nac_agent_delegations too (its array covers BOTH tables)" \
      "ERROR:  permission denied for table nac_agent_delegations" \
      "$(as host_recall "SELECT token FROM nac_agent_delegations" | head -1)"

# ── M-3: MEMBERSHIP is a bypass that survives an attribute re-assert ────────
# GRANT entity TO host_recall leaves rolsuper and rolbypassrls reading false
# while the connection can SET ROLE entity and read everything.
as entity "GRANT entity TO host_recall" >/dev/null
check "a granted membership DOES defeat the split (this is why it is revoked)" \
      "entity" \
      "$(docker exec "$CNAME" psql -h /var/run/postgresql -U host_recall -d hostmem -Atq \
           -c "SET ROLE entity; SELECT current_user" 2>&1 | tail -1)"
docker exec "$CNAME" psql -h /var/run/postgresql -U entity -d hostmem \
    -v ON_ERROR_STOP=1 -f /tmp/v139.sql >/dev/null 2>&1
check "re-running v139 REVOKES every role membership host_recall holds" "(none)" \
      "$(as entity "SELECT COALESCE(string_agg(g.rolname, ',' ORDER BY g.rolname),'(none)')
                      FROM pg_auth_members m JOIN pg_roles g ON g.oid=m.roleid
                     WHERE m.member=(SELECT oid FROM pg_roles WHERE rolname='host_recall')")"

# ── M-2: a SECURITY DEFINER function is a hole no policy can close ──────────
# It runs with its OWNER's privileges, and the owner here is the superuser that
# owns every table. PUBLIC gets EXECUTE on a new function by default, so one
# such function in `public` hands the restricted role the privileged lane back.
# There are zero today. This is a tripwire, so that the day somebody adds one it
# fails here instead of silently voiding the floor.
check "TRIPWIRE: no SECURITY DEFINER function exists in schema public" "0" \
      "$(as entity "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
                     WHERE p.prosecdef AND n.nspname='public'")"

# ── seed: 3 handler-only rows, 2 share-scoped ───────────────────────────────
# The tag convention is server/applications/memory_scope.py:21-27 — a memory
# WITHOUT a metadata.share_scope array is handler-only. That is the concept the
# policy below is keyed to, not a new one invented here.
as entity "INSERT INTO entity_memory (entity_id, content, memory_type, metadata) VALUES
    (1, 'handler only: bank balance',      'cited_fact', '{}'),
    (1, 'handler only: home address',      'cited_fact', '{\"emotion_tag\":\"neutral\"}'),
    (1, 'handler only: medical',           'cited_fact', '{}'),
    (1, 'shareable: product spec',         'cited_fact', '{\"share_scope\":[\"product_specs\"]}'),
    (1, 'shareable: list price',           'cited_fact', '{\"share_scope\":[\"pricing_book\"]}')" >/dev/null

# ── PHASE A — no policy: the two roles must be IDENTICAL ────────────────────
echo; echo "PHASE A — no policy installed (what v139 alone ships)"
sup_a="$(as entity      "SELECT count(*) FROM entity_memory")"
res_a="$(as host_recall "SELECT count(*) FROM entity_memory")"
check "superuser and host_recall see the SAME number of rows" "$sup_a" "$res_a"
check "...and the same rows, by id" \
      "$(as entity "SELECT string_agg(id::text, ',' ORDER BY id) FROM entity_memory")" \
      "$(as host_recall "SELECT string_agg(id::text, ',' ORDER BY id) FROM entity_memory")"
check "five rows were seeded" "5" "$sup_a"

# ── PHASE B — ONE policy, keyed to the share_scope concept ──────────────────
echo; echo "PHASE B — row security on, one SELECT policy for host_recall"
as entity "ALTER TABLE entity_memory ENABLE ROW LEVEL SECURITY" >/dev/null
as entity "CREATE POLICY host_recall_share_scoped ON entity_memory
             FOR SELECT TO host_recall
             USING (metadata ? 'share_scope'
                    AND jsonb_typeof(metadata->'share_scope') = 'array')" >/dev/null

check "the superuser still sees every row (RLS does not bind rolsuper/rolbypassrls)" "5" \
      "$(as entity "SELECT count(*) FROM entity_memory")"
check "host_recall sees ONLY the share-scoped rows" "2" \
      "$(as host_recall "SELECT count(*) FROM entity_memory")"
check "the hidden rows are hidden by CONTENT, not by luck" \
      "shareable: list price,shareable: product spec" \
      "$(as host_recall "SELECT string_agg(content, ',' ORDER BY content) FROM entity_memory")"

# The point of the whole design: it is not a filter the query has to remember.
check "a host_recall query that ASKS for a hidden row still gets nothing" "" \
      "$(as host_recall "SELECT content FROM entity_memory WHERE content LIKE '%bank balance%'")"
check "...not even through an aggregate that never names a column" "2" \
      "$(as host_recall "SELECT count(*) FROM entity_memory WHERE true")"
check "...and the rows do not exist for a subquery either" "" \
      "$(as host_recall "SELECT string_agg(content,',') FROM (SELECT content FROM entity_memory OFFSET 0) t WHERE content LIKE '%medical%'")"

# ── PHASE C — policy removed: identical again, and the ORDER matters ────────
echo; echo "PHASE C — policy dropped (the rollback path)"

# M-5. DROP POLICY first leaves a window in which row security is ON with no
# policy, and Postgres's default in that state is deny-all — so the Host's
# memory reads as EMPTY, not as an error, for however long the window lasts.
# Measured here rather than asserted, because the number is the argument.
as entity "DROP POLICY host_recall_share_scoped ON entity_memory" >/dev/null
check "WRONG ORDER (drop, then disable): the restricted lane sees NOTHING mid-window" "0" \
      "$(as host_recall "SELECT count(*) FROM entity_memory")"
as entity "ALTER TABLE entity_memory DISABLE ROW LEVEL SECURITY" >/dev/null

# The right order: disable first, and there is no window at all.
as entity "ALTER TABLE entity_memory ENABLE ROW LEVEL SECURITY" >/dev/null
as entity "CREATE POLICY host_recall_share_scoped ON entity_memory
             FOR SELECT TO host_recall
             USING (metadata ? 'share_scope'
                    AND jsonb_typeof(metadata->'share_scope') = 'array')" >/dev/null
as entity "ALTER TABLE entity_memory DISABLE ROW LEVEL SECURITY" >/dev/null
check "RIGHT ORDER (disable, then drop): every row is visible for the whole rollback" "5" \
      "$(as host_recall "SELECT count(*) FROM entity_memory")"
as entity "DROP POLICY host_recall_share_scoped ON entity_memory" >/dev/null

check "with the policy gone the two roles are identical again" \
      "$(as entity "SELECT string_agg(id::text, ',' ORDER BY id) FROM entity_memory")" \
      "$(as host_recall "SELECT string_agg(id::text, ',' ORDER BY id) FROM entity_memory")"

# ── PHASE D/E — the BOOT, driven by entrypoint.sh's own text ────────────────
# Everything above proves what the SQL does when it runs. These two prove it
# RUNS — which was the whole of M-1. The migration loop reads schema_migrations
# and `continue`s on an applied version, so anything reached through it executes
# once per host, ever: a re-assert written for drifted hosts would fire on none
# of them, and a restore that carries the table but not the role would skip it
# forever. So the entrypoint applies v139 from the UNCONDITIONAL position, and
# this phase extracts that section of entrypoint.sh AT RUN TIME and executes it.
# A gate carrying its own copy of the boot logic would keep passing while the
# entrypoint drifted, which is precisely the failure being closed.
boot() {
    sed -n '/^# ══════ Schema migrations ══════/,/^# ══════ Seed default skill files ══════/p' \
        "$REPO/entrypoint.sh" | sed '$d' > "/tmp/boot-block.$$.sh"
    docker cp "/tmp/boot-block.$$.sh" "$CNAME:/tmp/boot-block.sh" >/dev/null
    rm -f "/tmp/boot-block.$$.sh"
    docker exec -e PG_SOCKET=/var/run/postgresql -e ENTITY_DB=hostmem \
        "$CNAME" bash /tmp/boot-block.sh >/dev/null 2>&1
    return $?
}
docker exec "$CNAME" mkdir -p /app/migrations >/dev/null 2>&1
docker cp "$MIGRATION" "$CNAME:/app/migrations/v139_host_recall_role.sql" >/dev/null

echo; echo "PHASE D — a role drifted to SUPERUSER, and the boot that has to fix it"
as entity "ALTER ROLE host_recall SUPERUSER BYPASSRLS REPLICATION CREATEDB CREATEROLE" >/dev/null
as entity "GRANT entity TO host_recall" >/dev/null
# schema_migrations is created by the entrypoint, not by init-db.sql, so stand
# up the row the loop would read — that row IS the skip.
as entity "CREATE TABLE IF NOT EXISTS schema_migrations (
             version INTEGER PRIMARY KEY, name TEXT NOT NULL,
             applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW())" >/dev/null
as entity "INSERT INTO schema_migrations(version,name) VALUES (139,'v139_host_recall_role')
             ON CONFLICT (version) DO NOTHING" >/dev/null
check "drifted, and schema_migrations already says 139 (so the LOOP will skip it)" \
      "true|true|true|true|true|139" \
      "$(as entity "SELECT rolsuper||'|'||rolbypassrls||'|'||rolreplication||'|'||rolcreatedb||'|'||rolcreaterole
                           ||'|'||(SELECT string_agg(version::text,',' ORDER BY version) FROM schema_migrations)
                      FROM pg_roles WHERE rolname='host_recall'")"
boot
check "boot: the attributes came back" "false|false|false|false|false" \
      "$(as entity "SELECT rolsuper||'|'||rolbypassrls||'|'||rolreplication||'|'||rolcreatedb||'|'||rolcreaterole FROM pg_roles WHERE rolname='host_recall'")"
check "boot: the membership is gone with them" "(none)" \
      "$(as entity "SELECT COALESCE(string_agg(g.rolname, ',' ORDER BY g.rolname),'(none)')
                      FROM pg_auth_members m JOIN pg_roles g ON g.oid=m.roleid
                     WHERE m.member=(SELECT oid FROM pg_roles WHERE rolname='host_recall')")"
check "boot: SET ROLE entity is refused again" \
      'ERROR:  permission denied to set role "entity"' \
      "$(as host_recall "SET ROLE entity" | head -1)"

echo; echo "PHASE E — RESTORE: the dump carries schema_migrations, never the role"
# The platform's backup takes `pg_dump -Fc -Z0 --no-owner
# --no-privileges` of this ONE database and the entity lane takes no
# `pg_dumpall --globals-only` — so a restore into a fresh cluster has version
# 139 recorded, no host_recall, and no grants. Simulated exactly.
as entity "DROP OWNED BY host_recall" >/dev/null
as entity "DROP ROLE host_recall" >/dev/null
check "restored state: 139 applied, role ABSENT" "139|absent" \
      "$(as entity "SELECT (SELECT string_agg(version::text,',' ORDER BY version) FROM schema_migrations)
                           ||'|'|| CASE WHEN EXISTS (SELECT 1 FROM pg_roles WHERE rolname='host_recall')
                                        THEN 'present' ELSE 'absent' END")"
boot
check "boot: the role is created anyway, correct attributes" "false|false|false|false|false|true" \
      "$(as entity "SELECT rolsuper||'|'||rolbypassrls||'|'||rolcreatedb||'|'||rolcreaterole||'|'||rolreplication||'|'||rolcanlogin FROM pg_roles WHERE rolname='host_recall'")"
check "boot: the grants came back too (--no-privileges dropped them)" "SELECT" \
      "$(as entity "SELECT COALESCE(string_agg(DISTINCT privilege_type, ',' ORDER BY privilege_type),'(none)')
                      FROM information_schema.role_table_grants
                     WHERE grantee='host_recall' AND table_name='entity_memory'")"
check "boot: the recall lane opens" "1" "$(as host_recall "SELECT 1")"

# ── The two things v139 does NOT close, asserted so they cannot be forgotten ─
echo; echo "WHAT IS STILL OPEN — asserted, so a later change has to come back here"
check "THE TRUST HOLE: any client on this socket is still superuser 'entity'" \
      "entity|true" \
      "$(as entity "SELECT current_user||'|'||(SELECT rolsuper::text FROM pg_roles WHERE rolname=current_user)")"
check "the vector arm's SET LOCAL still works for host_recall (_vector_arm:152)" "10" \
      "$(docker exec "$CNAME" psql -h /var/run/postgresql -U host_recall -d hostmem -Atq \
           -c "BEGIN; SET LOCAL ivfflat.probes = 10; SELECT current_setting('ivfflat.probes'); COMMIT;" 2>&1)"

echo
if [[ $failed -eq 0 ]]; then
    green "ROLE-SPLIT PROOF PASSED"
    exit 0
fi
red "ROLE-SPLIT PROOF FAILED — $failed assertion(s)"
exit 1
