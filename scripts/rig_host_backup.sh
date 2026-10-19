#!/usr/bin/env bash
# rig_host_backup.sh — the Host's own backup and restore, end to end, on one docker box.
#
# What it proves (scripts/rig_host_backup_inner.py holds the checks): the
# runner and the restore from THIS tree (server/host_backup.py,
# server/host_restore.py), inside the kernel image built from this tree (its
# age, pg_dump and Python), locked down like a Host pod — uid 1000, read-only
# root, every capability dropped, no-new-privileges, the pods' own seccomp
# profile, and no network at all — against a real Host database. The platform
# (bridge ops and the presigned-URL storage) is a stand-in inside the same
# container, and so is the owner's browser (the age binary does what the
# vendored age library does in the page; scripts/test_settings_restore_browser.py
# proves the two open each other's files).
#
# Nothing here touches production: no network, a made-up recovery key, and a
# throwaway cseries-postgres container.
#
# Needs: docker, the kernel image built from this tree, cseries-postgres:smoke.
#   docker build -f Dockerfile.linux -t cseries-hb-rig:local .
#   scripts/rig_host_backup.sh [kernel image]
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
IMG="${1:-cseries-hb-rig:local}"
PG_IMG="${PG_IMG:-cseries-postgres:smoke}"
SCRATCH="${RIG_SCRATCH:-${TMPDIR:-/tmp}/cseries-hb-rig}"
mkdir -p "$SCRATCH"
W="$(mktemp -d "$SCRATCH/rig.XXXXXX")"
PG="hb-pg-$$"
cleanup() {
  docker stop "$PG" >/dev/null 2>&1 || true      # started with --rm: stopping removes it
  # The rig cleans up after itself: a shared scratch disk was filled once by
  # rig folders left behind. KEEP_RIG=1 keeps it for inspection.
  if [ "${KEEP_RIG:-0}" = 1 ]; then echo "rig files kept: $W"; else rm -rf -- "$W"; fi
}
trap cleanup EXIT
log() { printf '\n== %s\n' "$*"; }

log "a throwaway Host database (init-db.sql + every migration)"
mkdir -p "$W/sock" && chmod 777 "$W/sock"
docker run -d --rm --name "$PG" -v "$W/sock:/var/run/postgresql" "$PG_IMG" >/dev/null
for _ in $(seq 1 60); do docker exec "$PG" pg_isready -h /var/run/postgresql -U entity -d hostmem -q 2>/dev/null && break; sleep 1; done
docker cp "$REPO/init-db.sql" "$PG:/tmp/init-db.sql" >/dev/null
docker exec "$PG" psql -h /var/run/postgresql -U entity -d hostmem --single-transaction -q -f /tmp/init-db.sql >/dev/null 2>&1
docker cp "$REPO/migrations" "$PG:/tmp/migrations" >/dev/null
for f in $(ls "$REPO"/migrations/v*.sql | sort -V); do
  docker exec "$PG" psql -h /var/run/postgresql -U entity -d hostmem -v ON_ERROR_STOP=1 -q -f "/tmp/migrations/$(basename "$f")" >/dev/null
done
chmod 777 "$W/sock"/* 2>/dev/null || true

log "a Host apps tree with things that must and must not be backed up"
A="$W/apps"; mkdir -p "$A/app-state/user-files" "$A/proj/node_modules/x" "$A/crash" "$A/host-sdk/scratch" "$A/proj/.cache" "$A/proj/lib/vocabularies"
echo "my notes" > "$A/app-state/user-files/notes.txt"
echo "keep me" > "$A/proj/lib/vocabularies/core"   # a real file named core
echo "dep" > "$A/proj/node_modules/x/index.js"
echo "dump" > "$A/crash/core.4242"; echo "dump" > "$A/proj/core.1234"
echo "tmp" > "$A/host-sdk/scratch/t"; echo "c" > "$A/proj/.cache/c"
head -c 3000000 /dev/urandom > "$A/proj/blob.bin"   # does not compress: several upload parts
# A big file, so the writer's memory is measured streaming many parts.
head -c $((${RIG_BIG_MB:-96} * 1024 * 1024)) /dev/urandom > "$A/proj/big.bin"
mkdir -p "$A/proj/.git/objects" && ln "$A/proj/blob.bin" "$A/proj/.git/objects/blob-link"   # a hard link
chown -R 1000:1000 "$A"
mkdir -p "$W/store" && chown 1000:1000 "$W/store"   # the storage stand-in's objects

log "the runner and the restore, inside the kernel image, locked down like a Host pod"
docker run --rm --network none --user 1000:1000 --read-only --cap-drop ALL \
  --security-opt no-new-privileges --security-opt "seccomp=$REPO/seccomp-personal.json" \
  --tmpfs /tmp:rw,size=512m,uid=1000,gid=1000 -e RIG_BIG_MB="${RIG_BIG_MB:-96}" --tmpfs /home/privatae:rw,size=64m,uid=1000,gid=1000 \
  -v "$REPO:/work:ro" -v "$W/sock:/run/postgresql" -v "$A:/var/lib/cseries" -v "$W/store:/store" \
  -e PYTHONPATH=/work:/work/brain:/work/server -e HOME=/home/privatae \
  --entrypoint python3 "$IMG" /work/scripts/rig_host_backup_inner.py
