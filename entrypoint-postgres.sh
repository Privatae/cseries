#!/bin/sh
set -eu

# cseries-postgres — the entity's database container.
#
# Split out of the kernel image.
# The cut is CLUSTER vs SCHEMA:
#
#   this container owns the CLUSTER — initdb, postgresql.conf, the
#   density/capacity conf, the one-time TimescaleDB drop, and creating the
#   empty entity database. It owns $PGDATA; nothing else mounts it.
#
#   the kernel owns the SCHEMA — init-db.sql, the entity_registry seed and
#   /app/migrations/v*.sql all still run from entrypoint.sh over the shared
#   socket, because only the kernel holds the identity that ENTITY_ID comes
#   from and only the kernel image ships the migrations it needs.
#
# No secrets in this image and none in this file: the cluster is
# unix-socket-only (`listen_addresses = ''`, written below), the socket dir
# is an emptyDir shared with exactly one peer container in the same pod, and
# auth is `trust` because there is no network listener to authenticate to.

# Core dumps: BOUNDED, not banned — same change as the kernel entrypoint,
# same reasoning: a postmaster core is the USER's
# database contents in memory, so it is the user's artifact and the platform
# must neither read it nor destroy it. 2 GiB per dump, hard = soft. It matters
# more here than in the kernel container, because a postmaster core lands in
# $PGDATA on the persistent data volume.
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

# NO crash directory here, and that is the considered answer rather than an
# omission. Three facts decide it, all measured:
#
#  * The entity-data volume mounts AT $PGDATA (/app/pgdata, subPath pgdata) and
#    /app is the read-only image rootfs — so there is no writable SIBLING of
#    $PGDATA to put a crash dir in. The only writable place is inside $PGDATA.
#  * initdb refuses a non-empty directory ("initdb: error: directory exists but
#    is not empty"), so creating it before the initdb block below CrashLoops
#    every FRESH entity — silently, because stderr is discarded and set -eu
#    takes the exit. Reproduced on postgres:17-alpine.
#  * A directory inside $PGDATA is carried by pg_basebackup and any $PGDATA
#    copy, which is the opposite of what a crash artifact should do.
#
# And it bought nothing: the kernel container does not mount this volume, so
# /api/system/crash cannot list a postmaster core wherever it sits. So the
# dump stays where the kernel writes it — $PGDATA/core.<pid>, cwd of the
# postmaster — bounded at 2 GiB by the ulimit above, already excluded from the
# backup twice over (backup-entities.sh excludes pgdata wholesale from the data
# tar at :372, and core.[0-9]* by name), and belonging to the user.
#
# THE GAP, NAMED: a postgres core is not listable by the Host today. Closing it
# needs a cross-container read that does not exist. Do not "fix" that by moving
# the file somewhere the platform can read — that breaks the rule this whole
# change exists to keep.


PGDATA="${PGDATA:-/app/pgdata}"
PG_SOCKET="${PG_SOCKET:-/var/run/postgresql}"
# Maintenance postmasters (first-boot bootstrap, the TimescaleDB drop paths)
# bind HERE, never $PG_SOCKET. The kernel container polls the shared socket
# to decide the DB is ready; if a bootstrap server answered there, the kernel
# could start loading init-db.sql and then have the schema load killed
# mid-file by this script's `pg_ctl stop -m fast`. entity_registry is created
# near the TOP of init-db.sql, so a kill after that point leaves the sentinel
# saying "initialized" while most of the schema is missing — permanently, on
# every later boot. A private dir means the shared socket only ever carries
# the final `exec postgres`.
PG_BOOTSTRAP_SOCKET="${PG_BOOTSTRAP_SOCKET:-/tmp/pg-bootstrap}"
mkdir -p "$PG_BOOTSTRAP_SOCKET"

# ══════ The entity database's name ══════
# It was `parallax`. That is ALSO the name of a database on the platform's own
# Postgres cluster — two unrelated stores under one name, so the entity's own
# sovereign memory read as though it lived in the platform DB. New clusters
# are created as `hostmem`; existing ones are
# renamed in place by the one-time block further down.
ENTITY_DB_NEW="hostmem"
ENTITY_DB_OLD="parallax"
# Sentinel so the rename costs one maintenance start ONCE per volume, not a
# start/stop on every boot for the rest of the cluster's life.
DB_RENAME_STAMP="$PGDATA/.entity-db-renamed"

# ══════ The per-entity rename switch ══════
# Set to 0 by the operator for an entity whose KERNEL predates the both-names
# resolver in entrypoint.sh. That kernel waits on `psql -d parallax` and exits
# 1 after PG_WAIT_SECS, so renaming its database crashloops it — and rolling
# THIS image back does not recover, because the old entrypoint never recreates
# `parallax`. That is a one-way door, and this switch is what keeps it shut.
#
# Default 1, deliberately, even though "off" is the safe direction:
#   * the operator's gate is DEFAULT-DENY (it sets 0 unless it can positively
#     confirm the kernel is at or above the floor), so the dangerous case is
#     already covered where the decision is actually made, and
#   * defaulting to 0 here would mean the operator has to ADD an env to every
#     healthy entity to turn the rename on — a pod-template change on the whole
#     fleet, i.e. a fleet-wide recreate on the operator's next resume pass.
#     Defaulting to 1 means the common render is byte-identical to today's and
#     only the entities that must NOT rename carry an env.
# The operator decides this per entity when it renders the pod.
ENTITY_DB_RENAME="${ENTITY_DB_RENAME:-1}"

# Echo every entity database that exists on the server listening at $1, one per
# line, new name first. Empty output means neither answered.
list_entity_dbs() {
    _sock="$1"
    for _c in "$ENTITY_DB_NEW" "$ENTITY_DB_OLD"; do
        if psql -h "$_sock" -U entity -d "$_c" -Atqc 'SELECT 1' >/dev/null 2>&1; then
            echo "$_c"
        fi
    done
}

# Echo whichever entity database exists on the server listening at $1.
# Prefers the new name. Returns 1 if neither answers.
resolve_entity_db() {
    _first="$(list_entity_dbs "$1" | head -n1)"
    [ -n "$_first" ] || return 1
    echo "$_first"
}

# ══════ PostgreSQL: Initialize or start ══════
# One config writer for both branches below — the first-boot and reconcile
# blocks used to carry duplicate heredocs, and they drifted (a
# shared_preload_libraries gap). Idempotent: same file every boot.
#
# Sizing (density pass): per-entity DBs are ~300MB on the most
# active host; the OS page cache carries the working set, so a big
# shared_buffers just double-buffers pages ×50 entities/node. jit off —
# per-backend LLVM contexts cost memory and no per-entity query runs long
# enough to repay compilation.
#
# max_connections = 19 is a FLOOR with four named claimants, not a free pool:
#   brain pool ................ max  6   (server/brain_service.py BRAIN_POOL_KWARGS, min 1)
#   AI read lane (host_recall)  max  4   (same file RECALL_POOL_KWARGS, min 1)
#   bastion control-plane lane . max  2   (same file CONTROL_POOL_KWARGS, min 1, never pruned)
#   the Host's own schema ..... max  3   (WORLD_POOL_KWARGS max 2 plus one pg_dump or
#                                          psql for a snapshot or restore; the role's
#                                          CONNECTION LIMIT is 3, migrations/v233)
# That leaves 4 for the harness sidecar, diagnostics and every app in the host's
# Workbench COMBINED. 16 -> 19 for the fourth lane, so those 4 stay 4.
# Raise this number, not the pools, if a host needs more room. Measured:
# exhausting these 16 made bastion enrolment
# answer 502 with no log line anywhere, which is why the control lane
# was carved out of them rather than added on top — and the recall lane was
# carved the same way, out of the brain pool's former 10 rather than on top of
# it, so this number did not have to move.
write_pg_conf() {
    cat > "$PGDATA/postgresql.conf" <<PGCONF
listen_addresses = ''
unix_socket_directories = '$PG_SOCKET'
shared_buffers = 32MB
maintenance_work_mem = 64MB
effective_cache_size = 256MB
max_connections = 19
jit = off
logging_collector = off
log_destination = 'stderr'
log_min_messages = warning
max_wal_size = 256MB
min_wal_size = 64MB
# Bound what ONE replication slot can pin (the user-held memory mirror).
# A physical slot with no bound retains WAL forever while its consumer is gone,
# and the consumer here is a machine in someone's house that gets closed, moved
# and unplugged. Measured on this image: the default is -1, i.e.
# UNBOUNDED, against a 5Gi entity-data PVC on k3s local-path — so an offline
# bastion could fill the volume, and local-path being a hostPath bind, the NODE
# behind it and thus every other entity on that node.
#
# 1GB = 20% of the PVC and 4x max_wal_size, against per-entity DBs that measure
# ~300MB (the sizing note above). Past the bound PostgreSQL invalidates the slot
# (wal_status='lost') and frees the WAL: the mirror falls behind and has to
# re-seed with a fresh base backup, which is minutes on a 300MB database and is
# the correct trade against filling a shared node. Verified on this image: at a
# 64MB test bound the slot flipped to 'lost' and pg_wal stopped growing.
#
# This bound applies whether or not a mirror is configured; a slot that nothing
# is consuming is exactly the case it exists to survive.
max_slot_wal_keep_size = 1024MB
# Density trims: each entity runs its own postmaster, so idle
# worker-slot overhead multiplies by every warm entity on the node (50+
# target). Per-entity DBs are small — single-threaded maintenance is fine.
autovacuum_max_workers = 1
max_worker_processes = 6
# Memory BOUND: postgres must never use more than the limit. The container's
# cgroup limit is 512Mi (operator _postgres_container);
# the kernel enforces that as a hard cap already — this block makes the server's
# OWN worst case fit under it, so it can never be OOM-killed under adversarial
# concurrent load. Before: work_mem 16MB x hash_mem_multiplier 2 x parallel
# workers (2 per gather) x 16 connections, plus a parallel index build at
# 3 x maintenance_work_mem — above 512Mi on paper. After, worst case is
# roughly shared 32 + 16 backends x (8MB x 2 nodes) + autovacuum 32 +
# ~80 backend baseline = ~400MB. Measured on the busiest host: peak
# 49MB, so none of this bites normal use; parallel query buys nothing on a
# ~300MB per-entity database and only multiplies CPU across 50 hosts a node.
work_mem = 8MB
hash_mem_multiplier = 1.0
temp_buffers = 4MB
autovacuum_work_mem = 32MB
max_parallel_workers_per_gather = 0
max_parallel_maintenance_workers = 0
max_parallel_workers = 0
PGCONF
}

if [ ! -f "$PGDATA/PG_VERSION" ]; then
    echo "First boot — initializing PostgreSQL"
    initdb -D "$PGDATA" --auth=trust --no-locale --encoding=UTF8 -U entity >/dev/null 2>&1

    write_pg_conf
    # Bootstrap start is deliberately pg_ctl, not the exec below: `createdb`
    # needs a live server and the final postmaster must be PID 1's child, not
    # a daemon this script orphans.
    # The switch decides the name a FRESH cluster is born with too, not just
    # whether an existing one is renamed. An entity below the kernel floor whose
    # volume is new — a re-provision, a restored-from-nothing PVC — would
    # otherwise be born `hostmem` under a kernel that only knows how to wait for
    # `parallax`, which is the same crashloop the switch exists to prevent, just
    # reached from the other direction. So ENTITY_DB_RENAME=0 means "this entity
    # is on the old name", full stop.
    if [ "$ENTITY_DB_RENAME" = "1" ]; then
        _born="$ENTITY_DB_NEW"
    else
        _born="$ENTITY_DB_OLD"
    fi
    pg_ctl -D "$PGDATA" -l /tmp/pg_init.log start -w -o "-k $PG_BOOTSTRAP_SOCKET"
    createdb -h "$PG_BOOTSTRAP_SOCKET" -U entity "$_born"
    pg_ctl -D "$PGDATA" stop -m fast -w >/dev/null
    # Stamp ONLY when it was born with the new name — a cluster born on the old
    # one still has a rename ahead of it, on the boot after its kernel crosses
    # the floor and the switch flips.
    if [ "$_born" = "$ENTITY_DB_NEW" ]; then
        : > "$DB_RENAME_STAMP"
    fi
    echo "Cluster initialized — empty $_born database created (kernel loads the schema)"
else
    echo "Existing database found — reconciling config"
    # ── One-time TimescaleDB removal (packages purged) ──
    # Timescale carried zero hypertables fleet-wide but cost ~27MB of
    # background workers per pod. The live fleet converged through the
    # with-library drop on older images; this branch now exists for pgdata
    # that arrives from OUTSIDE the fleet timeline (a restored backup, a
    # thawed volume) whose conf still preloads timescale.
    #
    # Which drop shape is safe depends on whether the .so is present:
    #   * .so PRESENT (older image): must start PRELOADED to drop — the
    #     loader FATALs un-preloaded sessions while the library exists.
    #   * .so ABSENT (this image, post-purge): starting preloaded would
    #     itself FATAL ("could not access file"). But with no library
    #     there is no loader to object — strip the preload and DROP
    #     EXTENSION as plain catalog surgery. Leaving it installed is NOT
    #     an option: its event triggers reference the missing .so and
    #     every later migration's DDL would fail.
    if grep -q "shared_preload_libraries = 'timescaledb'" "$PGDATA/postgresql.conf" 2>/dev/null; then
        TS_SO_COUNT=$(ls /usr/lib/postgresql/17/lib/timescaledb*.so 2>/dev/null | wc -l)
        if [ "$TS_SO_COUNT" -gt 0 ]; then
            pg_ctl -D "$PGDATA" -l /tmp/pg_tsdrop.log start -w -o "-k $PG_BOOTSTRAP_SOCKET"
            if psql -h "$PG_BOOTSTRAP_SOCKET" -U entity -d "$(resolve_entity_db "$PG_BOOTSTRAP_SOCKET" || echo "$ENTITY_DB_OLD")" -v ON_ERROR_STOP=1 \
                   -c "DROP EXTENSION IF EXISTS timescaledb;" >/dev/null 2>&1; then
                echo "TimescaleDB extension dropped (one-time density trim)"
            else
                echo "WARNING: TimescaleDB drop failed — keeping preloading config this boot, will retry next boot" >&2
                cat /tmp/pg_tsdrop.log >&2 || true
                # Old (preloading) conf is still the on-disk file — the exec
                # below must start from it unchanged, so skip the rewrite.
                TS_DROP_DEFERRED=1
            fi
            pg_ctl -D "$PGDATA" stop -m fast -w >/dev/null
        else
            echo "TimescaleDB preload found but library purged from image — no-library drop path"
            write_pg_conf
            pg_ctl -D "$PGDATA" -l /tmp/pg_tsdrop.log start -w -o "-k $PG_BOOTSTRAP_SOCKET"
            if psql -h "$PG_BOOTSTRAP_SOCKET" -U entity -d "$(resolve_entity_db "$PG_BOOTSTRAP_SOCKET" || echo "$ENTITY_DB_OLD")" -v ON_ERROR_STOP=1 \
                   -c "DROP EXTENSION IF EXISTS timescaledb;" >/dev/null 2>&1; then
                echo "TimescaleDB extension dropped (no-library catalog drop)"
            else
                # Loud, actionable, and non-fatal to the boot: plain queries
                # work; only DDL (migrations) would hit the dangling triggers.
                echo "ERROR: TimescaleDB catalog drop failed on purged image — boot an older image that still has the library once to drop it" >&2
                cat /tmp/pg_tsdrop.log >&2 || true
            fi
            pg_ctl -D "$PGDATA" stop -m fast -w >/dev/null
        fi
    fi
    if [ -z "${TS_DROP_DEFERRED:-}" ]; then
        write_pg_conf
    fi

    # ── One-time rename: parallax -> hostmem ──
    # Runs HERE, on the BOOTSTRAP socket, for the reason at the top of this
    # file: the kernel decides the DB is ready by polling the SHARED socket,
    # and the shared socket only ever carries the final `exec postgres`. So
    # this always completes before anything can connect, and the rename needs
    # no separate migration pass and no downtime beyond the roll that shipping
    # this image already requires.
    #
    # NOT FATAL IF IT FAILS. A cosmetic rename must never take an entity down:
    # on any failure the database keeps its old name, and the kernel's
    # entrypoint probes for BOTH names and connects to whichever it finds.
    # The stamp is only written on a confirmed-good outcome, so a failed boot
    # simply retries next time.
    if [ "$ENTITY_DB_RENAME" != "1" ]; then
        # No maintenance start at all, and no stamp written — an entity whose
        # kernel later crosses the floor picks the rename up on that roll.
        echo "Entity database rename DISABLED for this entity (ENTITY_DB_RENAME=$ENTITY_DB_RENAME) — the database keeps whatever name it has"
    elif [ ! -f "$DB_RENAME_STAMP" ]; then
        pg_ctl -D "$PGDATA" -l /tmp/pg_rename.log start -w -o "-k $PG_BOOTSTRAP_SOCKET"
        _found="$(list_entity_dbs "$PG_BOOTSTRAP_SOCKET")"
        _found_n="$(printf '%s\n' "$_found" | grep -c . || true)"
        if [ "$_found_n" -ge 2 ]; then
            # Not producible by this script: the rename below only fires when
            # $ENTITY_DB_NEW does NOT exist, and a cluster born new is born with
            # $ENTITY_DB_NEW alone. Both present means something arrived from
            # outside that timeline (a restored dump loaded beside a live
            # database, a hand-created one). ALTER DATABASE would fail on the
            # name collision anyway, so there is nothing to do here but say so.
            #
            # Deliberately NO stamp: this warning repeats every boot until an
            # operator resolves it. The cost is one maintenance start per boot,
            # paid only by a cluster that is already in a state that should not
            # exist — the alternative is stamping it away and leaving a second
            # database nobody ever looks at again. The kernel's entrypoint picks
            # between them on evidence (which one carries the schema) and
            # refuses to boot if both do.
            echo "WARNING: BOTH $ENTITY_DB_NEW and $ENTITY_DB_OLD exist on this cluster — no rename is possible and none was attempted." >&2
            echo "         One of them is a stray. Confirm which carries this host's schema, drop or rename the other, then restart." >&2
        elif [ "$_found" = "$ENTITY_DB_OLD" ]; then
            # Connect to `postgres`, not to the database being renamed —
            # Postgres refuses to rename the database the session is in.
            if psql -h "$PG_BOOTSTRAP_SOCKET" -U entity -d postgres -v ON_ERROR_STOP=1 \
                   -c "ALTER DATABASE $ENTITY_DB_OLD RENAME TO $ENTITY_DB_NEW" >/dev/null 2>&1; then
                : > "$DB_RENAME_STAMP"
                echo "Entity database renamed $ENTITY_DB_OLD -> $ENTITY_DB_NEW"
            else
                echo "WARNING: rename $ENTITY_DB_OLD -> $ENTITY_DB_NEW failed — keeping the old name this boot, will retry next boot" >&2
                cat /tmp/pg_rename.log >&2 || true
            fi
        elif [ "$_found" = "$ENTITY_DB_NEW" ]; then
            : > "$DB_RENAME_STAMP"
        else
            echo "WARNING: neither $ENTITY_DB_NEW nor $ENTITY_DB_OLD found on an existing cluster — leaving it alone" >&2
        fi
        pg_ctl -D "$PGDATA" stop -m fast -w >/dev/null
    fi
fi

# ══════ The restore's load role, locked before anything can connect ══════
# server/host_restore.py loads a backup's rows as host_restore, a role that can
# log in only while a restore loads (_load_rows). A restore killed in the middle
# of its load (the machine off, a container stopped) leaves it able to log in,
# with its grants in the restore's database. So every boot puts it back first,
# in single-user mode, before the server listens on any socket: no login, no
# setting, a member of no role and with no member (host_restore.lock_load_role
# does the same at the start of every restore, and takes back its grants).
# Not fatal: a cluster this cannot reach still serves, and the next restore
# locks the role before anything else.
lock_restore_role() {
    _lock_sql="DO \$\$ DECLARE r record; BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'host_restore') THEN RETURN; END IF; ALTER ROLE host_restore NOLOGIN NOBYPASSRLS; ALTER ROLE host_restore RESET ALL; FOR r IN SELECT d.datname FROM pg_db_role_setting s JOIN pg_database d ON d.oid = s.setdatabase WHERE s.setrole = 'host_restore'::regrole LOOP EXECUTE format('ALTER ROLE host_restore IN DATABASE %I RESET ALL', r.datname); END LOOP; FOR r IN SELECT g.rolname AS granted, u.rolname AS member, w.rolname AS grantor FROM pg_auth_members m JOIN pg_roles g ON g.oid = m.roleid JOIN pg_roles u ON u.oid = m.member JOIN pg_roles w ON w.oid = m.grantor WHERE m.member = 'host_restore'::regrole OR m.roleid = 'host_restore'::regrole LOOP EXECUTE format('REVOKE %I FROM %I GRANTED BY %I CASCADE', r.granted, r.member, r.grantor); END LOOP; END \$\$;"
    if printf '%s\n' "$_lock_sql" | postgres --single -D "$PGDATA" postgres >/tmp/pg_lock_restore.log 2>&1 \
            && ! grep -q 'ERROR:' /tmp/pg_lock_restore.log; then
        return 0
    fi
    echo "WARNING: could not lock the restore role host_restore before serving; the next restore locks it first" >&2
    tail -20 /tmp/pg_lock_restore.log >&2 || true
}
lock_restore_role

# ══════ Peer-visible stats ══════
# The kernel's resources panel reads its OWN cgroup and its OWN /proc
# (server/routes/system.py). Since the split that means the database — the
# single biggest thing running next to it — vanished from the host's view of
# its own machine. There is no shared PID namespace (that isolation is
# deliberate) and a sibling cgroup is not readable from the
# kernel container, so the numbers have to be PUSHED: this container publishes
# its own into the socket emptyDir that both containers already mount. Same
# shape as the hub sidecar's loopback /status, minus the listener — this image
# is PG and nothing else, and it is going to stay that way.
#
# Line-oriented, not JSON: every value here comes from cgroupfs or /proc, and
# a process cmdline is arbitrary bytes. Hand-rolling JSON escaping in POSIX sh
# to carry attacker-influenceable text into the kernel's parser is a bug
# waiting to happen. So: `key value` per line, and for processes
# `proc <pid> <rss_bytes> <name>` with the free-form name LAST, where nothing
# it can contain changes the parse. Reader: _db_stats() in
# server/routes/system.py.
#
# Atomic (write tmp, rename in the same dir) so the kernel never sees a
# half-written file, and stamped with `ts` so a dead loop — or an older DB
# image that writes no file at all — reads as ABSENT rather than as
# fresh-but-wrong. The two images ship on independent cadences, so the kernel
# must degrade gracefully against a DB image that predates this.
STATS_FILE="$PG_SOCKET/.container-stats"
STATS_INTERVAL="${PG_STATS_INTERVAL:-5}"
STATS_MAX_PROCS="${PG_STATS_MAX_PROCS:-20}"

# ══════ Peer-visible build identity ══════
# Same push, different reason. The kernel cannot see which image this
# container runs: no shared PID or filesystem namespace, and the ref lives in
# the operator's render, not in the pod. It needs to know, because a user's
# bastion holds a STREAMING REPLICA of this cluster and a standby cannot
# replay WAL from a different PostgreSQL build — so the bastion has to be told
# exactly which artifact to run, and told by the pod rather than by guesswork.
#
# So publish the baked build-info into the same emptyDir the stats already
# use. One copy at boot, not a loop: the image cannot change under a running
# container. Atomic (write tmp, rename in the same dir) so a reader never
# sees a half-file; ABSENT is a legitimate state the kernel must handle,
# because an older DB image writes nothing here and the two images ship on
# independent cadences.
if [ -r /app/system/build-info.json ]; then
    cp /app/system/build-info.json "$PG_SOCKET/.db-build-info.tmp" 2>/dev/null \
        && mv -f "$PG_SOCKET/.db-build-info.tmp" "$PG_SOCKET/.db-build-info" 2>/dev/null \
        || echo "WARNING: could not publish build-info to $PG_SOCKET (mirror pinning will read as unknown)" >&2
fi

write_container_stats() {
    _page=$(getconf PAGESIZE 2>/dev/null || echo 4096)
    {
        echo "ts $(date +%s)"
        echo "interval $STATS_INTERVAL"
        # memory.max reads the literal string "max" when uncapped (the smoke
        # test's plain `docker run` has no limit) — pass it through verbatim
        # and let the reader decide; do not fake a number here.
        for _f in memory.current memory.max pids.current; do
            if [ -r "/sys/fs/cgroup/$_f" ]; then
                echo "$_f $(cat "/sys/fs/cgroup/$_f" 2>/dev/null)"
            fi
        done
        awk '$1=="inactive_file"||$1=="file"{print $1, $2}' \
            /sys/fs/cgroup/memory.stat 2>/dev/null
        awk '$1=="usage_usec"{print "cpu_usec", $2}' \
            /sys/fs/cgroup/cpu.stat 2>/dev/null
        # The database volume's room. The kernel container does not mount this
        # claim, so it cannot measure it. The Host's own schema turns read-only
        # when the volume is nearly full, so one runaway insert cannot stop the
        # Host writing its memory. POSIX df, 1K blocks. Reader:
        # brain/mastercode/world_db.py _db_room(). printf, not print: awk prints
        # a large number as 6.3e+10, which the reader cannot parse.
        df -P -k "$PGDATA" 2>/dev/null \
            | awk 'NR==2 {printf "pgdata_size %.0f\npgdata_free %.0f\n", $2 * 1024, $4 * 1024}'
        _n=0
        for _p in /proc/[0-9]*; do
            [ "$_n" -lt "$STATS_MAX_PROCS" ] || break
            _rss=$(awk '{print $2}' "$_p/statm" 2>/dev/null) || continue
            case "$_rss" in ''|*[!0-9]*) continue ;; esac
            # cmdline reads far better than comm ("postgres: walwriter" vs a
            # 15-char truncation); NULs become spaces, and any newline is
            # stripped because one would forge a record in this format.
            _name=$(tr '\0\n\r' '   ' < "$_p/cmdline" 2>/dev/null | cut -c1-96)
            case "$_name" in
                '' | ' '*) _name=$(tr -d '\n\r' < "$_p/comm" 2>/dev/null) ;;
            esac
            [ -n "$_name" ] || continue
            # RSS counts every SHARED page in full, in every process that maps
            # it — and the biggest thing each PostgreSQL backend maps is the
            # shared buffer cache. Measured on a live Host: four
            # idle backends read 77 MB of RSS between them while their private
            # pages came to 8 MB, and summing this container's RSS gave 152 MB
            # against a cgroup charged 60 MB. The kernel's panel was showing
            # the 77 MB. So publish PSS (this process's fair share of the pages
            # it shares) and PRIVATE (what actually frees if it exits) beside
            # RSS, from smaps_rollup — one pre-summed block per process, not a
            # walk of every mapping.
            #
            # A NEW record, not a wider `proc`: this image and the kernel image
            # ship on independent cadences, so an older kernel has to keep
            # parsing what it already understands. It reads `proc` and ignores
            # `procx`; a newer kernel prefers `procx` and falls back to `proc`.
            # Both are emitted for one release, after which `proc` can go.
            _pss=$(awk '/^Pss:/{print $2 * 1024; exit}' "$_p/smaps_rollup" 2>/dev/null)
            _priv=$(awk '/^Private_Clean:|^Private_Dirty:/{s += $2} END{print s * 1024}' \
                    "$_p/smaps_rollup" 2>/dev/null)
            echo "proc ${_p#/proc/} $((_rss * _page)) $_name"
            # Emit procx only when BOTH reads produced a plain number —
            # smaps_rollup is absent on pre-4.14 kernels and unreadable across
            # a uid boundary, and a half-filled record would parse as a zero.
            case "$_pss" in ''|*[!0-9]*) _pss="" ;; esac
            case "$_priv" in ''|*[!0-9]*) _priv="" ;; esac
            if [ -n "$_pss" ] && [ -n "$_priv" ]; then
                echo "procx ${_p#/proc/} $((_rss * _page)) $_pss $_priv $_name"
            fi
            _n=$((_n + 1))
        done
    } > "$STATS_FILE.tmp" 2>/dev/null && mv -f "$STATS_FILE.tmp" "$STATS_FILE" 2>/dev/null
}

# `set +e` inside the subshell on purpose: this loop is decoration on a
# database container. Every read in it is best-effort, and no failure in it
# may ever take the postmaster down.
(
    set +e
    while :; do
        write_container_stats
        sleep "$STATS_INTERVAL"
    done
) &

# ══════ Stale rename stamp: self-heal ══════
# The stamp is what buys us "no maintenance postmaster on any later boot", and
# that matters here specifically: this DB can run on a slow SATA drive (one
# measured at ~24 fsync IOPS with a p99 of 143ms while idle). A start/stop per
# boot is not free on such a disk. So the stamp stays.
#
# But a stamp can outlive the thing it asserts. The documented recovery for an
# entity that renamed under an old kernel is `ALTER DATABASE hostmem RENAME TO
# parallax` from inside this container — after which the stamp still says "done"
# and the rename block never runs again, so the entity is stranded on the old
# name forever even once its kernel is fine. An operator would have to know to
# delete a dotfile in $PGDATA.
#
# Checked HERE rather than in the rename block because the block is exactly what
# the stamp skips: verifying it up there costs the maintenance start the stamp
# exists to avoid. Down here the real postmaster is already coming up on the
# shared socket, so the check is one query and it costs nothing. It only ever
# DELETES the stamp — the rename itself still happens in the guarded block on
# the next boot, on the bootstrap socket, where it is safe.
(
    set +e
    [ -f "$DB_RENAME_STAMP" ] || exit 0
    _n=0
    while [ "$_n" -lt 60 ]; do
        psql -h "$PG_SOCKET" -U entity -d postgres -Atqc 'SELECT 1' >/dev/null 2>&1 && break
        sleep 1
        _n=$((_n + 1))
    done
    _have="$(psql -h "$PG_SOCKET" -U entity -d postgres -Atqc \
        "SELECT string_agg(datname, ',' ORDER BY datname) FROM pg_database \
         WHERE datname IN ('$ENTITY_DB_NEW','$ENTITY_DB_OLD')" 2>/dev/null)"
    # Only the unambiguous case. An empty read means the query failed, not that
    # the databases are gone, and must never delete the stamp.
    if [ "$_have" = "$ENTITY_DB_OLD" ]; then
        rm -f "$DB_RENAME_STAMP"
        echo "WARNING: rename stamp said $ENTITY_DB_NEW but this cluster only has $ENTITY_DB_OLD." >&2
        echo "         Stamp cleared — the rename will be retried on the next boot." >&2
        echo "         (Expected after a hand rollback; otherwise something renamed it back.)" >&2
    fi
) &

# ══════ Serve ══════
# postgres in the FOREGROUND as tini's child: the container's liveness IS
# the postmaster's liveness, so a dead DB restarts the DB container instead
# of leaving a pod whose kernel talks to a socket nobody is listening on.
# The stats loop above is a child of THIS pid, which exec replaces — it rides
# along under tini and dies with the container.
echo "Starting PostgreSQL (unix socket: $PG_SOCKET)"
exec postgres -D "$PGDATA" -k "$PG_SOCKET"
