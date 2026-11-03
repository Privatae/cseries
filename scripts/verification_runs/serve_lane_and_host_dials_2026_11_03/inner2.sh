#!/usr/bin/env bash
# Runs inside: unshare --pid --net --mount --fork --mount-proc  ("the server", 203.0.113.1)
# A C-Series Host (this branch) at home behind a NAT router, which the server cannot reach, connects
# to the S2 bastion on the server: pair through the window, lanes, heartbeat, a grant, a model.
set -euo pipefail
TREE=$1; W=$2; BAST=$3
log() { printf '%s e2e %s\n' "$(date -u +%H:%M:%S.%3NZ)" "$*"; }
mount --make-rprivate /
ip link set lo up
mount -t tmpfs tmpfs /run
mount -t tmpfs tmpfs /tmp
mkdir -p /run/netns
rm -rf "$W/etc-up" "$W/etc-work"; mkdir -p "$W/etc-up" "$W/etc-work"
mount -t overlay overlay -o lowerdir=/etc,upperdir="$W/etc-up",workdir="$W/etc-work" /etc
mount -t tmpfs tmpfs /etc/systemd/system
mount -t tmpfs tmpfs /var/lib/privatae
mount -t tmpfs tmpfs /var/lib/cseries
mkdir -p /etc/cseries/keys
cp "$TREE/etc/cseries-apps.pub.raw" "$TREE/etc/community-apps.pub.raw" "$TREE/etc/ceigas_root_v2.crt" /etc/cseries/keys/
mount -t tmpfs tmpfs /app
for d in server brain dashboard cli native-bundles native-sdk default-skills migrations docs scripts system-apps; do
  mkdir -p "/app/$d" && mount --bind "$TREE/$d" "/app/$d" && mount -o remount,bind,ro "/app/$d"
done
mkdir -p /app/designs/builtin && mount --bind "$TREE/designs/builtin" /app/designs/builtin && mount -o remount,bind,ro /app/designs/builtin
cp "$TREE/init-db.sql" /app/init-db.sql
mkdir -p /app/system/apps /app/workspace /app/projects /app/ceigas /app/portal /app/user-files /app/data /app/secrets /app/integrations /app/engine /app/training_memory_jobs

log "network: server 203.0.113.1 | router 203.0.113.2, NAT | home 192.168.1.10 (the Host)"
ip netns add router; ip netns add home
ip link add srv0 type veth peer name wan0 netns router
ip addr add 203.0.113.1/24 dev srv0 && ip link set srv0 up
ip -n router link set lo up
ip -n router addr add 203.0.113.2/24 dev wan0 && ip -n router link set wan0 up
ip -n router link add lan0 type veth peer name eth0 netns home
ip -n router addr add 192.168.1.1/24 dev lan0 && ip -n router link set lan0 up
ip -n home link set lo up
ip -n home addr add 192.168.1.10/24 dev eth0 && ip -n home link set eth0 up
ip -n home route add default via 192.168.1.1
ip netns exec router sysctl -qw net.ipv4.ip_forward=1
ip netns exec router nft -f - <<'NFT'
table ip nat {
	chain post {
		type nat hook postrouting priority 100;
		oifname "wan0" masquerade
	}
}
table ip filter {
	chain forward_home {
		type filter hook forward priority 0; policy drop;
		ct state established,related accept
		iifname "lan0" oifname "wan0" accept
	}
}
NFT
ip route add 192.168.1.0/24 via 203.0.113.2
sleep 1

log "the stand-in model server on the server's own 127.0.0.1:8080"
python3 -I "$W/standin_openai2.py" 127.0.0.1 8080 >"$W/standin.log" 2>&1 &

log "the C-Series Host (python3 -m server.launch, this branch) at home"
mkdir -p "$W/hostdir" "$W/host-home"
HOSTENV=(env -i PATH=/usr/local/bin:/usr/bin:/bin HOME="$W/host-home" PYTHONPATH="/app:/app/brain"
  DATABASE_URL="$(cat "$W/DSN")" ENTITY_ID=1 PRIVATAE_HOST_DIR="$W/hostdir" TM_JOBS_ROOT="$W/tm-jobs")
start_host() { ( cd /app && exec ip netns exec home "${HOSTENV[@]}" DASHBOARD_BIND_HOST=127.0.0.1 DASHBOARD_PORT=8800 \
    "$W/venv/bin/python" -m server.launch ) >>"$W/host.log" 2>&1 & echo $! >"$W/host.pid"; }
start_host
for _ in $(seq 300); do (ip netns exec home ss -ltn | grep -q ':8811 ') && break; sleep 0.5; done
ip netns exec home ss -ltn | grep -q ':8811 ' || { log "FAIL: the Host did not come up"; tail -40 "$W/host.log"; exit 1; }
if timeout 3 bash -c '</dev/tcp/192.168.1.10/8811' 2>/dev/null; then log "FAIL: the server reached the Host's lane port"; exit 1; fi
log "ok: the Host is up at home, and the server cannot reach it (192.168.1.10:8811 behind the NAT)"

export HOME="$W/server-home" XDG_CONFIG_HOME="$W/server-home/.config"
mkdir -p "$XDG_CONFIG_HOME"
unset PRIVATAE_LLM_ADDR OLLAMA_ADDR
log "on the server: privatae-bastion pair --open"
"$BAST" pair --open >"$W/open.out" 2>&1 &
OPEN=$!
for _ in $(seq 100); do grep -q 'code:' "$W/open.out" && break; sleep 0.1; done
cat "$W/open.out"
sed -n 's/^  code: *//p' "$W/open.out" | head -1 >"$W/CODE"
[ -s "$W/CODE" ] || { log "FAIL: no code"; exit 1; }
touch "$W/WINDOW"
log "window open; the person adds the server on their Host (waiting for $W/PAIRED)"
while [ ! -e "$W/PAIRED" ]; do sleep 0.5; done
wait $OPEN || true
sed -n '/Paired\|paired\|code:/,$p' "$W/open.out" | tail -8
start_bastion() { "$BAST" run >>"$W/bastion.log" 2>&1 & echo $! >"$W/bastion.pid"; }
log "the bastion runs on the server"
start_bastion
touch "$W/RUNNING"
while [ ! -e "$W/DONE" ]; do
  if [ -e "$W/RESTART_BASTION" ]; then rm -f "$W/RESTART_BASTION"; kill "$(cat "$W/bastion.pid")"; sleep 1; start_bastion; log "bastion restarted"; fi
  if [ -e "$W/OPEN2" ]; then rm -f "$W/OPEN2"
    log "on the server, a second window: privatae-bastion pair --open (the bastion is running)"
    "$BAST" pair --open >"$W/open2.out" 2>&1 &
    for _ in $(seq 100); do grep -q 'code:' "$W/open2.out" && break; sleep 0.1; done
    sed -n 's/^  code: *//p' "$W/open2.out" | head -1 >"$W/CODE2"; cat "$W/open2.out"; touch "$W/WINDOW2"; fi
  if [ -e "$W/UNPAIR" ]; then h=$(cat "$W/UNPAIR"); rm -f "$W/UNPAIR"
    log "on the server: privatae-bastion unpair $h"; "$BAST" unpair "$h" 2>&1 | tail -3; fi
  if [ -e "$W/RESTART_HOST" ]; then rm -f "$W/RESTART_HOST"; pkill -P "$(cat "$W/host.pid")" || true; kill "$(cat "$W/host.pid")" 2>/dev/null || true; sleep 2; start_host; log "Host restarted"; fi
  sleep 0.5
done
log "stopped"
