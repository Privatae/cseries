#!/bin/sh
set -eu

# Core dumps: BOUNDED, not banned.
#
# This was once `ulimit -c 0`, after a crashing SDK CLI
# (V8 fatal OOM) wrote 4x ~6GB core.<pid> files into a Host's apps volume.
# That stopped the volume from filling, and it also destroyed the
# artifact outright — which is the wrong half of the trade. A core dump is a
# full process memory image: it is the USER's data, the platform can never look
# at it, and it is the user's own troubleshooting material. Deleting it to save
# disk is the same act as a kernel-level discard handler, one layer up. So: keep
# the volume safe WITHOUT destroying the evidence.
#
#   2 GiB per dump, hard = soft so nothing in the tree can raise it back.
#     ABOVE the bundled CLI's own V8 heap ceiling (the since-deleted brain/mastercode/
#     runtime/host_sdk_engine.py set NODE_HEAP_OPTS = --max-old-space-size=1536), so the one
#     crash class actually measured here yields a COMPLETE dump rather than a
#     truncated one gdb cannot open. BELOW the retained budget, so one dump can
#     never exhaust it alone.
#   4 GiB retained across the crash dir, newest-wins — systemd-coredump's
#     documented MaxUse ceiling ("10% of the total disk size, capped at 4GiB",
#     coredump.conf(5)). Enforced in server/crash.py, which also relocates
#     dumps here and is what the Host reads through /api/system/crash.
#
# ulimit is inherited by every child (server, SDK CLI, harness subprocesses).
# ulimit -c takes BLOCKS and the block size is implementation-defined. Measured
# Dash (which /bin/sh IS in this image) uses 512-byte blocks, bash
# uses 1024 — so a hardcoded block count means 2 GiB in one shell and 1 GiB in
# the other, silently. That matters here specifically: the bound is sized to sit
# ABOVE the bundled CLI's V8 heap ceiling (1536 MB) so the crash class we have
# actually measured yields a COMPLETE dump; at 1 GiB it would be truncated and
# useless. So probe rather than assume.
#
# Three things this has to survive, each of which broke a previous version:
#   * `ulimit -S -c 1` moves the SOFT limit only. A bare `ulimit -c 1` would
#     drop the HARD limit to one block, and a non-root process can never raise
#     a hard limit again.
#   * Column $5 of /proc/self/limits is the soft value; reading $6 (hard)
#     returns "unlimited" and falls through to the default.
#   * NEVER exceed the inherited hard limit, and NEVER divide by zero. As
#     non-root under an outer hard limit of 1 GiB, asking for 2 GiB fails and
#     leaves the soft limit at the 1-block probe value — 512 BYTES, i.e. no
#     usable dump at all, silently. And an outer hard limit of 0 made $_core_blk
#     0, so the arithmetic below aborted the script under `set -eu`: a
#     CrashLoop on every pod whose parent set RLIMIT_CORE 0. Measured both.
#     The entity pod's PID 1 inherits unlimited/unlimited today
#     (`grep 'Max core file size' /proc/1/limits` in the entity container),
#     so neither case is live — but a bound that depends on nobody ever
#     setting an outer limit is not a bound.
CORE_TARGET_BYTES=2147483648            # 2 GiB
_core_hard=$(awk '/Max core file size/ {print $6}' /proc/self/limits 2>/dev/null)
ulimit -S -c 1 2>/dev/null || true
_core_blk=$(awk '/Max core file size/ {print $5}' /proc/self/limits 2>/dev/null)
case "$_core_blk" in ''|*[!0-9]*|0) _core_blk=1024 ;; esac
_core_want=$CORE_TARGET_BYTES
case "$_core_hard" in
    ''|*[!0-9]*) : ;;                                   # unlimited / unreadable
    *) [ "$_core_hard" -lt "$_core_want" ] && _core_want=$_core_hard ;;
esac
if [ "$_core_want" -ge "$_core_blk" ]; then
    ulimit -c $((_core_want / _core_blk)) 2>/dev/null || true
else
    # An outer policy tighter than one block. Honour it rather than leaving the
    # soft limit at the probe value.
    ulimit -S -c 0 2>/dev/null || true
fi

# Same override knob as server/crash.py DATA_ROOT, so the two can never point at
# different directories.
CSERIES_CRASH_ROOT="${CSERIES_CRASH_ROOT:-/var/lib/cseries}"
CRASH_DIR="$CSERIES_CRASH_ROOT/crash"
mkdir -p "$CRASH_DIR" 2>/dev/null || true

# NO emergency floor here, deliberately. This used to keep "the two newest"
# dumps and delete the rest on every boot. That is COUNT-based, and it silently
# contradicted the 4 GiB budget the rest of the system advertises (the API
# returns retain_max_bytes, the SOP tells the user "the newest 4 GiB is kept"):
# a user with three small dumps would lose the oldest to a routine image roll,
# having been told it would be kept. Destroying a user's artifact to enforce a
# limit that is not the stated limit is the exact failure this ticket is about.
#
# Retention is size-based and lives in ONE place: server/crash.py
# enforce_retention(), run from the boot sweep (main.py lifespan calls
# crash.sweep_at_boot()) and after every relocate. If the server cannot start,
# deleting the user's dumps does not help it start.

CERT_DIR="/etc/privatae/certs"
PERSIST_DIR="/etc/privatae/certs-persistent"
# The DB lives in the cseries-postgres container now; this process only ever
# talks to it over the shared socket dir. $PGDATA is deliberately absent —
# nothing kernel-side may touch the data directory.
PG_SOCKET="/var/run/postgresql"

# ══════ Identity: restore or self-provision ══════

# Detect boot mode: persistent volume → env vars → self-provision
has_env=false
for var in RELAY_CONFIG RELAY_CERT RELAY_KEY CA_CERT; do
    eval val=\${$var:-}
    [ -n "$val" ] && has_env=true && break
done

if [ -f "$PERSIST_DIR/relay.json" ] && [ -s "$PERSIST_DIR/relay.json" ]; then
    # Path 1: Certs exist from prior boot — restore
    echo "Restoring identity from persistent volume"
    cp "$PERSIST_DIR/relay.json" "$PERSIST_DIR/relay.crt" \
       "$PERSIST_DIR/relay.key" "$PERSIST_DIR/ceigas_ca.crt" "$CERT_DIR/" 2>/dev/null || true
    [ -f "$PERSIST_DIR/integrations.json" ] && cp "$PERSIST_DIR/integrations.json" "$CERT_DIR/"
    # C-Series Gateway server cert (mTLS listener) — restore if present
    [ -f "$PERSIST_DIR/server.crt" ] && cp "$PERSIST_DIR/server.crt" "$CERT_DIR/server.crt"
    [ -f "$PERSIST_DIR/server.key" ] && cp "$PERSIST_DIR/server.key" "$CERT_DIR/server.key"

elif [ "$has_env" = true ]; then
    # Path 2: First boot with env vars (enterprise/managed deployment)
    echo "Provisioning from environment variables"
    echo "$RELAY_CONFIG" | base64 -d > "$CERT_DIR/relay.json" 2>/dev/null
    echo "$RELAY_CERT" | base64 -d > "$CERT_DIR/relay.crt" 2>/dev/null
    echo "$RELAY_KEY" | base64 -d > "$CERT_DIR/relay.key" 2>/dev/null
    echo "$CA_CERT" | base64 -d > "$CERT_DIR/ceigas_ca.crt" 2>/dev/null
    # C-Series Gateway server cert (optional — may arrive on a later provision
    # pass once the entity's synaptive handle is known).
    if [ -n "${SERVER_CERT_PEM:-}" ] && [ -n "${SERVER_KEY_PEM:-}" ]; then
        echo "$SERVER_CERT_PEM" | base64 -d > "$CERT_DIR/server.crt" 2>/dev/null
        echo "$SERVER_KEY_PEM"  | base64 -d > "$CERT_DIR/server.key" 2>/dev/null
        chmod 0600 "$CERT_DIR/server.key" 2>/dev/null || true
    fi
    # Persist for future boots
    if [ -d "$PERSIST_DIR" ]; then
        cp "$CERT_DIR/relay.json" "$CERT_DIR/relay.crt" \
           "$CERT_DIR/relay.key" "$CERT_DIR/ceigas_ca.crt" "$PERSIST_DIR/"
        [ -f "$CERT_DIR/server.crt" ] && cp "$CERT_DIR/server.crt" "$PERSIST_DIR/server.crt"
        [ -f "$CERT_DIR/server.key" ] && cp "$CERT_DIR/server.key" "$PERSIST_DIR/server.key"
        echo "Certs persisted for future updates"
    fi
    unset RELAY_CONFIG RELAY_CERT RELAY_KEY CA_CERT SERVER_CERT_PEM SERVER_KEY_PEM

else
    # Path 3: No certs, no env vars — a standalone Host on its own machine.
    # It is the only Host in its database, so it takes the row init-db.sql
    # creates (id 1). Id 0 is not used: the schema reserves it for the
    # platform's own row, and loops such as the scheduled-task runner only
    # start for an id above 0.
    echo "No platform identity — starting as a standalone Host"
    STANDALONE_MODE=1
fi

# --- Permissions + validation ---
if [ "${STANDALONE_MODE:-0}" = "1" ]; then
    ENTITY_ID=1
else
    chmod 600 "$CERT_DIR/relay.key" 2>/dev/null || true
    chmod 644 "$CERT_DIR/relay.crt" "$CERT_DIR/ceigas_ca.crt" "$CERT_DIR/relay.json" 2>/dev/null || true

    python3 -c "
from cryptography import x509
from cryptography.hazmat.primitives.serialization import load_pem_private_key
import sys
try:
    x509.load_pem_x509_certificate(open('$CERT_DIR/relay.crt', 'rb').read())
    load_pem_private_key(open('$CERT_DIR/relay.key', 'rb').read(), password=None)
    x509.load_pem_x509_certificate(open('$CERT_DIR/ceigas_ca.crt', 'rb').read())
except Exception as e:
    print(f'ERROR: Certificate validation failed: {e}', file=sys.stderr); sys.exit(1)
" || exit 1

    ENTITY_ID=$(python3 -c "import json; print(json.load(open('$CERT_DIR/relay.json'))['entity_id'])")
    # Validate entity_id is numeric (prevent SQL injection via env/config)
    case "$ENTITY_ID" in
        ''|*[!0-9]*) echo "ERROR: Invalid ENTITY_ID: $ENTITY_ID" >&2; exit 1 ;;
    esac
fi
echo "Personal computer starting for entity $ENTITY_ID"
export ENTITY_ID

# ══════ PostgreSQL: wait for the DB container ══════
# The server no longer runs in THIS container. It runs in `cseries-postgres`,
# a peer container in the same pod that owns $PGDATA and shares the
# run-pgsock emptyDir mounted here at $PG_SOCKET (initdb, the conf writer,
# the density conf and the TimescaleDB drop surgery all live in that image's
# entrypoint-postgres.sh).
#
# Peer containers start CONCURRENTLY — k8s orders init containers, not
# these — so the kernel waits instead of assuming a server it started
# itself. The wait is BOUNDED on purpose: an unbounded one turns a dead DB
# container into a pod that hangs forever with no crashloop to flag it,
# which is exactly the ":8443 silently dark" failure this repo has already
# paid for once.
#
# pg_isready alone is not sufficient: it reports "server accepting
# connections" before the entity database has been created on a first boot, so
# the readiness signal is a real query against the real database. Do NOT
# "simplify" this to probe `postgres` instead — that database exists from
# initdb onward and would go green while the entity's own database did not yet
# exist, which is the exact bug this wait was written to fix.
#
# WHICH database is resolved here rather than hardcoded. The entity DB was
# renamed `parallax` -> `hostmem`: `parallax` is ALSO a
# database on the platform's own Postgres cluster, so one name covered two
# unrelated stores and the entity's own sovereign memory read as though it
# lived in the platform DB. cseries-postgres renames it in place at boot, but a
# pod whose rename has not happened (or did not succeed) must still come up —
# so probe both names, prefer the new one, and use whatever answered for the
# rest of this script. Nothing below may hardcode either name.
PG_WAIT_SECS="${PG_WAIT_SECS:-180}"
waited=0
ENTITY_DB_PRESENT=""
while [ -z "$ENTITY_DB_PRESENT" ]; do
    for _cand in hostmem parallax; do
        if pg_isready -h "$PG_SOCKET" -U entity -d "$_cand" -q \
           && psql -h "$PG_SOCKET" -U entity -d "$_cand" -Atq -c 'SELECT 1' >/dev/null 2>&1; then
            ENTITY_DB_PRESENT="${ENTITY_DB_PRESENT}${_cand} "
        fi
    done
    [ -n "$ENTITY_DB_PRESENT" ] && break
    if [ "$waited" -ge "$PG_WAIT_SECS" ]; then
        echo "ERROR: PostgreSQL (cseries-postgres container) not ready after ${PG_WAIT_SECS}s" >&2
        echo "       probed for databases: hostmem, parallax" >&2
        pg_isready -h "$PG_SOCKET" -U entity >&2 || true
        exit 1
    fi
    [ "$waited" -eq 0 ] && echo "Waiting for PostgreSQL (cseries-postgres container) on $PG_SOCKET"
    sleep 1
    waited=$((waited + 1))
done

# BOTH names answering is not a state cseries-postgres can produce — its rename
# runs only when `hostmem` does not already exist, and a cluster born new is
# born as `hostmem` alone. It means something arrived from OUTSIDE that
# timeline: a restored dump loaded next to a live database, a hand-created
# database, two backups merged onto one volume.
#
# Preferring the new name blindly here is the one silent-data-loss shape this
# whole rename can produce: if the host's memory is in `parallax` and an empty
# `hostmem` sits beside it, the schema probe below finds nothing, loads
# init-db.sql into the empty one, and the host boots with no memory at all
# while every row it ever wrote is still on disk one database over. So the
# choice is made on EVIDENCE (which database carries the schema), never on the
# name, and the genuinely ambiguous case refuses rather than guesses.
if [ "$ENTITY_DB_PRESENT" = "hostmem parallax " ]; then
    _hm_schema="$(psql -h "$PG_SOCKET" -U entity -d hostmem -Atq -c \
        "SELECT to_regclass('public.entity_registry') IS NOT NULL" 2>/dev/null || true)"
    _px_schema="$(psql -h "$PG_SOCKET" -U entity -d parallax -Atq -c \
        "SELECT to_regclass('public.entity_registry') IS NOT NULL" 2>/dev/null || true)"
    if [ "$_hm_schema" = "t" ] && [ "$_px_schema" = "t" ]; then
        echo "ERROR: both 'hostmem' and 'parallax' exist on this cluster AND both carry a" >&2
        echo "       loaded schema. Refusing to guess which one holds this host's memory:" >&2
        echo "       choosing wrong would write new turns into the wrong store and split it" >&2
        echo "       in two, which no later boot could unpick. Resolve by hand — keep one," >&2
        echo "       drop or rename the other — then restart this pod." >&2
        exit 1
    fi
    if [ "$_px_schema" = "t" ]; then
        ENTITY_DB="parallax"
    else
        ENTITY_DB="hostmem"
    fi
    echo "WARNING: both 'hostmem' and 'parallax' exist on this cluster. Using '$ENTITY_DB'" >&2
    echo "         because that is the one carrying the schema. The other is a stray and" >&2
    echo "         nothing will ever read it — drop it once you have confirmed that." >&2
else
    ENTITY_DB="${ENTITY_DB_PRESENT% }"
fi
export ENTITY_DB
echo "PostgreSQL ready (unix socket: $PG_SOCKET, database: $ENTITY_DB)"
if [ "$ENTITY_DB" = "parallax" ]; then
    # Not fatal: the entity works fine either way. Loud because it means the
    # rename in cseries-postgres did not run or did not succeed.
    echo "NOTE: still on the legacy database name 'parallax' — cseries-postgres renames it to 'hostmem' at boot; check that container's log" >&2
fi

# ══════ A restore waiting to be swapped in ══════
# Settings > Backup > Restore stages the restored database and files while the
# Host runs, then restarts this container (server/host_restore.py). The swap
# happens HERE: before the schema step, so migrations run forward over the
# restored database, and before the server starts, so nothing holds either
# database while they are renamed. It takes seconds; the slow part is done.
#
# What protects the Host if it cannot run on the restored data is the PLAN,
# not this shell: swap() counts every boot that ends before the kernel says it
# started (host_restore.mark_started, called from server/main.py), and the
# third such boot puts the previous database and files back. The trap below is
# only a shortcut for a boot that this shell itself ends with an error: it
# undoes at once instead of after three tries. It cannot see anything after
# `exec` (the shell is gone), so it is disarmed right before it; and a SIGTERM
# (the kubelet stopping the pod) is not a failure of the restored data, so it
# exits 143 and undoes nothing, and the boot counts like any other.
_restore_armed=0
if [ -f /var/lib/cseries/.host-restore/plan.json ]; then
    _restore_rc=0
    python3 -m server.host_restore swap "$ENTITY_DB" || _restore_rc=$?
    if [ "$_restore_rc" -eq 0 ]; then
        _restore_armed=1
        trap 'exit 143' TERM
        trap '_rc=$?; if [ "$_rc" -ne 0 ] && [ "$_rc" -ne 143 ]; then python3 -m server.host_restore undo "$ENTITY_DB" "the Host did not start on the restored data (exit $_rc)" || true; fi' EXIT
    fi
fi

# The kernel's schema step is every line between the two "kernel schema
# section" markers. scripts/kernel_schema.py RUNS those lines, with dash, to
# build the schema every DB gate tests on, so a gate and a booting Host run the
# same text. The section reads its files under $KERNEL_SCHEMA_APP and leaves its
# two logs under $KERNEL_SCHEMA_TMP, defaulting to /app and /tmp: nothing sets
# either at boot, and a gate points them at the tree under test. A schema step
# written outside the markers is one no gate runs.
# >>> kernel schema section >>>
# ══════ Schema: base load + migrations ══════
# The KERNEL owns its schema — the DB container creates the empty entity
# database ($ENTITY_DB, resolved above) and stops there. So init-db.sql and
# /app/migrations/v*.sql both run from here, over the shared socket.
#
# THIS IS WHY Dockerfile.linux STILL INSTALLS postgresql-client-17 even
# though the server moved out. Do not "finish the split" by stripping the
# client: the schema lane below is the kernel's, and every in-container gate
# (scripts/test_*.py, scripts/smoke_test.sh) also reaches the DB with psql
# from this container — which is what proves the shared socket works.
#
# The old first-boot gate was `[ ! -f $PGDATA/PG_VERSION ]`; the kernel no
# longer mounts $PGDATA, so the gate is now the schema's own presence.
#
# CRITICAL: the probe must distinguish "the table is absent" from "I could
# not ask". A connection-level failure (the DB container restarting in the
# gap after the wait loop) also yields empty stdout, and treating that as
# "empty database" re-runs init-db.sql over a POPULATED one — which inserts
# a SECOND active entity_registry row (the identity anchor) while every
# error is swallowed, and the outcome-verify below then reads 't' and lets
# the boot proceed green. So: trust psql's EXIT STATUS, and only accept a
# literal 't'/'f' answer. A probe that cannot answer is retried, then fails
# the boot — never silently reinterpreted.
schema_present=""
probe_tries=0
while [ "$probe_tries" -lt 10 ]; do
    if probe_out="$(psql -h "$PG_SOCKET" -U entity -d "$ENTITY_DB" -Atq \
                    -c "SELECT to_regclass('public.entity_registry') IS NOT NULL" 2>${KERNEL_SCHEMA_TMP:-/tmp}/schema-probe.err)"; then
        case "$probe_out" in
            t|f) schema_present="$probe_out"; break ;;
        esac
    fi
    probe_tries=$((probe_tries + 1))
    sleep 2
done
if [ -z "$schema_present" ]; then
    echo "ERROR: could not determine schema state after ${probe_tries} probes —" >&2
    echo "       refusing to guess (guessing 'empty' would re-run init-db.sql" >&2
    echo "       over a populated database)." >&2
    tail -5 ${KERNEL_SCHEMA_TMP:-/tmp}/schema-probe.err >&2 || true
    exit 1
fi

if [ "$schema_present" != "t" ]; then
    echo "Empty database — loading base schema"
    # ATOMIC (--single-transaction): the load either lands whole or not at
    # all. Without it, anything that kills the kernel mid-file (eviction,
    # OOM, a DB restart) leaves a PARTIAL schema — and because
    # entity_registry is created near the top of init-db.sql, the sentinel
    # above would then read "initialized" forever while most tables are
    # missing, with migrations crashlooping and no path back. A rollback
    # leaves a genuinely empty database that simply re-loads next boot.
    # Verified against this image: init-db.sql contains no non-transactional
    # statement (no CONCURRENTLY/VACUUM/CREATE DATABASE) and loads clean in
    # one transaction — 0 errors, 57 tables.
    # Still NOT ON_ERROR_STOP: inside a single transaction the first error
    # aborts the rest anyway, so the COMMIT degrades to ROLLBACK and the
    # outcome-verify below fails the boot loudly — which is the behavior we
    # want, without changing how psql reports each statement.
    psql -h "$PG_SOCKET" -U entity -d "$ENTITY_DB" --single-transaction \
         -f ${KERNEL_SCHEMA_APP:-/app}/init-db.sql >${KERNEL_SCHEMA_TMP:-/tmp}/init-db.log 2>&1 || true
    if [ "$(psql -h "$PG_SOCKET" -U entity -d "$ENTITY_DB" -Atq -c \
            "SELECT to_regclass('public.entity_registry') IS NOT NULL" 2>/dev/null)" != "t" ]; then
        echo "ERROR: init-db.sql did not create the base schema — refusing to serve" >&2
        tail -40 ${KERNEL_SCHEMA_TMP:-/tmp}/init-db.log >&2 || true
        exit 1
    fi

    # Set the entity's actual ID to match the platform identity.
    # Skip in genesis mode — genesis client sets ID after linking.
    if [ "$ENTITY_ID" -gt 0 ] 2>/dev/null; then
        # The id goes to psql as a variable (:'eid'), never spliced into the SQL
        # text; psql reads variables only from a script, so the SQL comes on stdin.
        psql -h "$PG_SOCKET" -U entity -d "$ENTITY_DB" -v ON_ERROR_STOP=1 \
            -v eid="$ENTITY_ID" >/dev/null <<'SQL'
UPDATE entity_registry SET id = :'eid'::int, privatae_entity_id = :'eid'::bigint WHERE id = 1;
SELECT setval('entity_registry_id_seq', :'eid'::bigint);
SQL
    fi

    echo "Database initialized — schema loaded, entity $ENTITY_ID seeded"
fi

# ══════ Schema migrations ══════
# Idempotent SQL scripts under /app/migrations/v{NNN}_*.sql apply schema
# changes that postdate init-db.sql for this entity. Each runs once;
# applied versions are recorded in schema_migrations. Failures here
# abort startup — loud failure beats silent breakage in the chat path.
psql -h "$PG_SOCKET" -U entity -d "$ENTITY_DB" -v ON_ERROR_STOP=1 -c \
    "CREATE TABLE IF NOT EXISTS schema_migrations (
        version    INTEGER     PRIMARY KEY,
        name       TEXT        NOT NULL,
        applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );" >/dev/null

# ══════ The recall role, asserted on EVERY boot ══════
# v139 creates `host_recall` and grants it SELECT — the restricted lane the AI's
# read path connects on, so a row security policy has something to bind to.
#
# It runs HERE, unconditionally, and NOT through the loop below. A role is not
# schema. The loop is a once-per-host mechanism: it reads schema_migrations and
# `continue`s on an applied version, so a numbered migration executes exactly
# once in a host's life. That is right for a CREATE TABLE and wrong for a role,
# in both directions:
#
#   DRIFT — v139's re-assert exists to pull a role that has been hand-altered to
#   SUPERUSER BYPASSRLS back into line. Through the loop it would run on no host
#   that had already applied it, i.e. on no host it was written for. Measured on
#   a throwaway cluster: drift the role, boot again, the loop printed nothing and
#   the role stayed superuser.
#
#   RESTORE — roles are CLUSTER-level, schema_migrations is a TABLE. The host
#   backup is `pg_dump --no-owner --no-privileges` of this one database
#   (the platform's entity backup); the entity lane takes no
#   `pg_dumpall --globals-only`. So a restore carries "139 applied" and carries
#   neither the role nor the grants. The loop then skips v139, host_recall does
#   not exist, brain_service's create_pool fails, get_recall_pool() returns None
#   and the read path silently drops back to the superuser lane behind one
#   warning. Measured: FATAL: role "host_recall" does not exist.
#
# Recording 139 in the same step keeps the loop from applying it a second time on
# a fresh host, and keeps `schema_migrations` an honest account of what has run.
# Fatal on failure, exactly like the loop: a host that cannot assert this role
# must not come up quietly serving from the privileged lane.
RECALL_ROLE_SQL=${KERNEL_SCHEMA_APP:-/app}/migrations/v139_host_recall_role.sql
if [ -f "$RECALL_ROLE_SQL" ]; then
    if ! psql -h "$PG_SOCKET" -U entity -d "$ENTITY_DB" -v ON_ERROR_STOP=1 \
            -f "$RECALL_ROLE_SQL"; then
        echo "ERROR: could not assert the host_recall role — refusing to serve" >&2
        exit 1
    fi
    psql -h "$PG_SOCKET" -U entity -d "$ENTITY_DB" -c \
        "INSERT INTO schema_migrations(version, name)
         VALUES (139, 'v139_host_recall_role') ON CONFLICT (version) DO NOTHING" >/dev/null
fi

# ══════ The Host's own schema, asserted on EVERY boot, AFTER the loop ══════
# v233 makes `host_world` and `world`. Like v139 it is a role and its grants, so it runs on every boot, not
# once per host. Unlike v139 it runs after the loop below: its grants are an
# explicit list reconciled against the whole schema, including a table a
# migration created in this same boot. Recorded first so the loop skips it.
WORLD_ROLE_SQL=${KERNEL_SCHEMA_APP:-/app}/migrations/v233_host_world.sql
if [ -f "$WORLD_ROLE_SQL" ]; then
    psql -h "$PG_SOCKET" -U entity -d "$ENTITY_DB" -c \
        "INSERT INTO schema_migrations(version, name)
         VALUES (233, 'v233_host_world') ON CONFLICT (version) DO NOTHING" >/dev/null
fi

if [ -d ${KERNEL_SCHEMA_APP:-/app}/migrations ]; then
    for f in ${KERNEL_SCHEMA_APP:-/app}/migrations/v*.sql; do
        [ -f "$f" ] || continue
        base=$(basename "$f" .sql)
        # v001_foo → 1
        version=$(echo "$base" | sed -E 's/^v0*([0-9]+).*$/\1/')
        case "$version" in
            ''|*[!0-9]*) echo "ERROR: migration $base has no version number" >&2; exit 1 ;;
        esac
        # The version and name go to psql as variables, never spliced into the SQL.
        applied=$(printf '%s\n' "SELECT 1 FROM schema_migrations WHERE version = :'ver'::int;" \
            | psql -h "$PG_SOCKET" -U entity -d "$ENTITY_DB" -At -v ON_ERROR_STOP=1 \
                -v ver="$version" 2>/dev/null)
        if [ "$applied" = "1" ]; then
            continue
        fi
        echo "Applying migration: $base"
        if ! psql -h "$PG_SOCKET" -U entity -d "$ENTITY_DB" -v ON_ERROR_STOP=1 -f "$f"; then
            echo "ERROR: migration $base failed" >&2
            exit 1
        fi
        printf '%s\n' "INSERT INTO schema_migrations(version, name) VALUES (:'ver'::int, :'name');" \
            | psql -h "$PG_SOCKET" -U entity -d "$ENTITY_DB" -v ON_ERROR_STOP=1 \
                -v ver="$version" -v name="$base" >/dev/null
    done
fi

# Fatal on failure, like v139: a Host whose world role cannot be asserted must not
# come up with a role whose grants are whatever they were last boot.
if [ -f "$WORLD_ROLE_SQL" ]; then
    if ! psql -h "$PG_SOCKET" -U entity -d "$ENTITY_DB" -v ON_ERROR_STOP=1 \
            -f "$WORLD_ROLE_SQL"; then
        echo "ERROR: could not assert the host_world role — refusing to serve" >&2
        exit 1
    fi
fi
# <<< kernel schema section <<<

# ══════ A restore brings back data, never older authorization ══════
# The restored database has just been migrated. Before anything serves, the
# access state that was live until the swap (bastion tokens and revocations,
# management grants, the second factor, backup keys...) replaces what the
# backup held: server/host_restore.py CARRIED_TABLES. Once per restore; a
# failure stops this boot, and the trap puts the previous data back.
if [ "$_restore_armed" = 1 ]; then
    python3 -m server.host_restore carry "$ENTITY_DB"
fi

# Whatever the readiness probe above actually found — never a literal.
export DATABASE_URL="postgresql://entity@/${ENTITY_DB}?host=$PG_SOCKET"

# The AI's READ lane: same database, same socket, the restricted role that
# migrations/v139 creates (NOSUPERUSER, NOBYPASSRLS). It exists so a row
# security policy on entity_memory has something to bind to; `entity` carries
# rolbypassrls and would ignore one. Exported HERE rather than derived in Python
# for the same reason DATABASE_URL is built here — $ENTITY_DB was resolved by
# probing, and nothing below may hardcode either name.
#
# Ordering is why this line can be unconditional: the role assertion above runs
# on EVERY boot, not once per host, and it exits the boot if it fails — so by
# the time control reaches here the role exists with the attributes it is meant
# to have. server/brain_service.py still falls back to the privileged lane if the
# role is somehow absent, so an entity running an image older than that assertion
# answers rather than dies.
export RECALL_DATABASE_URL="postgresql://host_recall@/${ENTITY_DB}?host=$PG_SOCKET"

# The Host's own schema: the role v233 asserts above, every boot. The `data` tool
# runs the model's SQL on this lane and NEVER falls back to `entity`
# (server/brain_service.py get_world_pool).
export WORLD_DATABASE_URL="postgresql://host_world@/${ENTITY_DB}?host=$PG_SOCKET"

# ══════ Seed default skill files ══════
# Skills are .md rule files the host (and its harness pods) read.
# At BOOT we SEED only skills whose frontmatter has no
# `present_if_app_installed:` field (or has `present_if_app_installed:
# always`). App-gated skills are seeded by the application installer
# AFTER the gating app is installed — see
# server/applications/skill_seeding.py.
#
# User edits take precedence — but UNEDITED defaults track the image.
# Seed-if-absent alone meant a shipped skill improvement
# silently never reached any existing pod (the same staleness class as
# the native-bundle trap the ship runbook documents). So each seed
# records the sha256 it seeded in .seeded/<name>.sha256:
#   - file == image default            -> current; (re)stamp the marker
#   - marker exists, file == marker    -> unedited old default; REFRESH
#   - marker exists, file != marker    -> user-edited; never touched
#   - no marker, file != default       -> pre-marker legacy: refresh, but
#     keep the old copy alongside as .pre-refresh-<date> (recoverable if
#     it was ever a real edit; markers make this case vanish going forward)
#
# The refresh applies to GATED skills too, once they are
# present. Until now a gated skill was seeded once at install and never
# looked at again: one Host's claude-sdk.md was the old text
# ("research(query) — single-shot Claude SDK call with WebSearch +
# WebFetch") three months after the image's copy was rewritten, and the
# host repeated it to the user as the reason research failed. Seeding a
# gated skill that is ABSENT stays the installer's job (the app may not
# be installed); refreshing one that is PRESENT is this loop's job.
#
# Two legacy shapes are handled here as well, because nothing else ever
# touches an existing pod's skills dir:
#   - renames: an image default that replaced an older filename (the
#     claude-sdk.md -> claude-code.md rename) adopts the old file as its
#     dest so the normal refresh rules apply. The mv below is
#     UNCONDITIONAL, so both cases end up under the NEW name: an unedited
#     file is refreshed to the image copy and the old name is gone; an
#     EDITED file is carried over under the new name with the user's edits
#     intact and left alone by the refresh (marker != content), which
#     means the new default is NOT seeded beside it and nothing remains
#     under the old name (measured by running this block against a seeded
#     temp dir, edited and unedited).
#   - retired: a gated skill whose harness no longer exists (copilot.md —
#     the Copilot harness is retired; coding-tutor.md — the VS Code editor
#     and its harness are removed) is moved aside as
#     <name>.retired-<date>, never deleted. Only names listed here; a
#     user's own skill files are never touched.
SKILL_RENAMES="claude-sdk.md:claude-code.md freeagent.md:bastion-agent.md"
SKILL_RETIRED="copilot.md coding-tutor.md"
if [ -d /app/default-skills ]; then
    mkdir -p /var/lib/cseries/skills/.seeded
    for pair in $SKILL_RENAMES; do
        old="/var/lib/cseries/skills/${pair%%:*}"
        new="/var/lib/cseries/skills/${pair##*:}"
        if [ -e "$old" ] && [ ! -e "$new" ]; then
            mv "$old" "$new"
            mv "/var/lib/cseries/skills/.seeded/${pair%%:*}.sha256" \
               "/var/lib/cseries/skills/.seeded/${pair##*:}.sha256" 2>/dev/null || true
            echo "Renamed legacy skill file: ${pair%%:*} -> ${pair##*:} (refresh rules apply below)"
        fi
    done
    for name in $SKILL_RETIRED; do
        if [ -e "/var/lib/cseries/skills/$name" ] && [ ! -f "/app/default-skills/$name" ]; then
            mv "/var/lib/cseries/skills/$name" "/var/lib/cseries/skills/$name.retired-$(date +%Y%m%d)"
            echo "Retired skill file: $name (its harness no longer exists; kept as $name.retired-$(date +%Y%m%d))"
        fi
    done
    for f in /app/default-skills/*.md; do
        [ -f "$f" ] || continue
        # Read frontmatter (everything between the first two `---` lines)
        # and decide whether to seed. awk pulls the block, grep checks
        # for a gating field.
        gate=$(awk '/^---/{c++; if(c==2)exit; next} c==1 && /^present_if_app_installed:/ {print $2}' "$f" 2>/dev/null)
        base=$(basename "$f")
        dest="/var/lib/cseries/skills/$base"
        marker="/var/lib/cseries/skills/.seeded/$base.sha256"
        src_sha=$(sha256sum "$f" | cut -d' ' -f1)
        if [ ! -e "$dest" ]; then
            if [ -n "$gate" ] && [ "$gate" != "always" ]; then
                # Gated and absent — the installer seeds it after the app installs.
                continue
            fi
            cp "$f" "$dest"
            echo "$src_sha" > "$marker"
            echo "Seeded default skill: $base (always-present)"
            continue
        fi
        dest_sha=$(sha256sum "$dest" | cut -d' ' -f1)
        if [ "$dest_sha" = "$src_sha" ]; then
            echo "$src_sha" > "$marker"   # current — ensure the marker exists
            continue
        fi
        if [ -f "$marker" ] && [ "$(cat "$marker")" != "$dest_sha" ]; then
            continue                       # user-edited — theirs, always
        fi
        if [ ! -f "$marker" ]; then
            cp "$dest" "$dest.pre-refresh-$(date +%Y%m%d)"   # legacy one-time backup
        fi
        cp "$f" "$dest"
        echo "$src_sha" > "$marker"
        echo "Refreshed default skill: $base (unedited default tracked the image)"
    done
    chown -R privatae:privatae /var/lib/cseries/skills 2>/dev/null || true
fi

# ══════ Embeddings — bridge-hosted ══════
# Entity pods do not run Ollama. All
# embedding calls route through the bridge embeddings.embed op to the
# platform's hosted Ollama, or to the configured backend. See
# brain/mastercode/memory_engine.py:_embed_via_bridge.

# ══════ Gitea credentials ══════
# Entities are provisioned with a per-entity gitea API token. When
# present, wire it into ~/.git-credentials so any `git push` / `git pull`
# against $GITEA_URL uses it transparently. Identity also rewrites
# user.name / user.email to entity-specific values so commits attribute
# correctly. No-op if GITEA_API_TOKEN is unset (genesis-mode entity, or
# a deployment without the gitea integration).
if [ -n "${GITEA_API_TOKEN:-}" ] && [ -n "${GITEA_USERNAME:-}" ] && [ -n "${GITEA_URL:-}" ]; then
    HOST=$(echo "$GITEA_URL" | sed -E 's,^https?://,,; s,/$,,')
    HOME_DIR="${HOME:-/home/privatae}"
    mkdir -p "$HOME_DIR"
    echo "https://${GITEA_USERNAME}:${GITEA_API_TOKEN}@${HOST}" > "$HOME_DIR/.git-credentials"
    chmod 600 "$HOME_DIR/.git-credentials"
    git config --global credential.helper store
    git config --global user.name  "${GITEA_USERNAME}"
    git config --global user.email "${GITEA_USERNAME}@privatae.ai"
    git config --global init.defaultBranch main
    echo "Gitea credentials configured for ${GITEA_USERNAME} → ${HOST}"
fi

# ══════ Start FastAPI ══════
#
# Two listeners on the same FastAPI app:
#
#   :8800  HTTP   — dashboard + legacy synaptive_edge HTTP /mcp path
#                   (TLS terminates at Caddy on Privatae-FW; this leg is
#                    HTTP within trusted infrastructure)
#
#   :8443  HTTPS+mTLS — served by the in-pod mTLS FRONT TERMINATOR
#                   (server/mtls_frontdoor.py), NOT uvicorn directly. Caddy
#                   SNI-passthroughs `<handle>.<host zone>` to this port;
#                   TLS terminates HERE. The terminator verifies the CEIGAS
#                   client cert, extracts its CN, and reverse-proxies to the
#                   loopback dashboard (::1:8800) injecting a trusted
#                   X-Client-Cert-CN header. This lets the app gate the dashboard
#                   bypass to the ROUTER cert only (Layer 2 of the cross-tenant
#                   fix) — uvicorn 0.34 cannot surface the peer cert to the app,
#                   so a terminator is required.
#
# The mTLS terminator is gated on server cert presence so existing
# containers without a provisioned server cert come up cleanly with just
# the HTTP listener until they're reprovisioned.

# The terminator runs IN-PROCESS as a supervised asyncio task inside uvicorn
# (server/main.py lifespan → mtls_frontdoor.run_supervised) — same pattern as
# the :8444 peer transport. It used to be a separate python3 process spawned
# from a shell supervisor loop here; that cost a full second interpreter
# (~33-64MB/pod, measured) for code that is pure asyncio
# anyway. The in-process task keeps the supervisor's guarantees: restarts on
# crash with backoff, waits for a (re)provisioned cert instead of giving up
# at boot, and logs loudly — a silently dark :8443 once cost a long debug
# (every bastion loses the pod with no crashloop to flag it).

# Bind dual-stack (::) — entity pods are IPv6-only, and the kubelet's
# liveness/readiness probes dial the pod's IPv6 address. 0.0.0.0 here made
# every pipeline image crash-loop in k8s (probe connection refused → kill).
#
# This used to exec `uvicorn --host ::` directly, under a comment claiming
# "Kernel bindv6only=0 keeps IPv4 docker reachability working". It did not:
# asyncio sets IPV6_V6ONLY on the socket when it resolves a hostname to
# AF_INET6, overriding the sysctl, so the listener was IPv6-ONLY on every
# image ever built. The image's own HEALTHCHECK dials 127.0.0.1, so every
# container ran permanently `(unhealthy)`, and a published `-p 127.0.0.1:…`
# port always reset the connection. server/launch.py binds the socket with
# V6ONLY cleared and hands it to uvicorn, which makes that claim true rather
# than aspirational. IPv6 — the path the kubelet probes actually use — is
# unchanged; this only ADDS the IPv4 path.
# The restore trap cannot outlive this shell (see "A restore waiting" above).
if [ "$_restore_armed" = 1 ]; then trap - EXIT TERM; fi
exec python3 -m server.launch
