#!/usr/bin/env bash
# Runs inside: unshare --pid --net --mount --fork --mount-proc. This branch's Host
# (python3 -m server.launch) on this namespace's own 127.0.0.1:8800, its kernel schema in a
# throwaway cseries-postgres; then Chromium with a virtual authenticator (browser.py).
set -euo pipefail
TREE=$1; W=$2; VENV=$3; SHOTS=$4
log() { printf '%s run %s\n' "$(date -u +%H:%M:%S)" "$*"; }
mount --make-rprivate /
ip link set lo up
mount -t tmpfs tmpfs /run
mount -t tmpfs tmpfs /tmp
rm -rf "$W/etc-up" "$W/etc-work"; mkdir -p "$W/etc-up" "$W/etc-work"
mount -t overlay overlay -o lowerdir=/etc,upperdir="$W/etc-up",workdir="$W/etc-work" /etc
mount -t tmpfs tmpfs /var/lib/cseries 2>/dev/null || { mkdir -p /var/lib/cseries && mount -t tmpfs tmpfs /var/lib/cseries; }
mkdir -p /etc/cseries/keys
cp "$TREE/etc/cseries-apps.pub.raw" "$TREE/etc/community-apps.pub.raw" "$TREE/etc/ceigas_root_v2.crt" /etc/cseries/keys/
mount -t tmpfs tmpfs /app
for d in server brain dashboard cli native-bundles native-sdk default-skills migrations docs scripts system-apps; do
  mkdir -p "/app/$d" && mount --bind "$TREE/$d" "/app/$d" && mount -o remount,bind,ro "/app/$d"
done
mkdir -p /app/designs/builtin && mount --bind "$TREE/designs/builtin" /app/designs/builtin && mount -o remount,bind,ro /app/designs/builtin
cp "$TREE/init-db.sql" /app/init-db.sql
mkdir -p /app/system/apps /app/workspace /app/projects /app/ceigas /app/portal /app/user-files /app/data /app/secrets /app/integrations /app/engine /app/training_memory_jobs
mkdir -p "$W/hostdir" "$W/host-home"
HOSTENV=(env -i PATH=/usr/local/bin:/usr/bin:/bin HOME="$W/host-home" PYTHONPATH="/app:/app/brain"
  DATABASE_URL="$(cat "$W/DSN")" ENTITY_ID=1 PRIVATAE_HOST_DIR="$W/hostdir" TM_JOBS_ROOT="$W/tm-jobs")
# The Host in a network namespace of its own ("vm", 10.77.0.2), as in its VM, its 8800 forwarded to
# this machine's 127.0.0.1:8800 the way npx cseries's QEMU forward publishes it: the browser is then
# a peer from outside, which a session (not loopback) lets in.
mkdir -p /run/netns
ip netns add vm
ip link add m0 type veth peer name eth0 netns vm
ip addr add 10.77.0.1/30 dev m0 && ip link set m0 up
ip -n vm link set lo up && ip -n vm addr add 10.77.0.2/30 dev eth0 && ip -n vm link set eth0 up
log "the C-Series Host (this branch) in the vm namespace, 10.77.0.2:8800"
( cd /app && exec ip netns exec vm "${HOSTENV[@]}" DASHBOARD_BIND_HOST=0.0.0.0 DASHBOARD_PORT=8800 "$VENV/bin/python" -m server.launch ) >"$W/host.log" 2>&1 &
for _ in $(seq 300); do (ip netns exec vm ss -ltn | grep -q ':8800 ') && break; sleep 0.5; done
socat TCP-LISTEN:8800,bind=127.0.0.1,reuseaddr,fork TCP:10.77.0.2:8800 &
sleep 0.5
for _ in $(seq 100); do curl -s -o /dev/null http://127.0.0.1:8800/api/owner/status && break; sleep 0.3; done
curl -s http://127.0.0.1:8800/api/owner/status; echo
log "Chromium with a virtual authenticator"
BEAT=("${HOSTENV[@]}" "$VENV/bin/python" "$TREE/scripts/verification_runs/passkey_approvals_2026_11_03/beat.py")
printf '%q ' "${BEAT[@]}" >"$W/beat.cmd"
( cd /app && HOME=/root python3 -I "$TREE/scripts/verification_runs/passkey_approvals_2026_11_03/browser.py" "$W" "$SHOTS" ) 2>&1 | tee "$W/browser.log"
log "done"
