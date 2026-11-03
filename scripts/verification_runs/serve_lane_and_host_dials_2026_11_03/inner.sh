#!/usr/bin/env bash
# Runs inside: unshare --pid --net --mount --fork --mount-proc   ("the machine")
# The machine runs the real bastion and a stand-in OpenAI-compatible server on ITS 127.0.0.1:8080.
# The C-Series Host (this branch's code) runs in a network namespace of its own ("vm"), as in its VM:
# it can't reach the machine's loopback; its enroll and lane ports are forwarded to the machine's
# 127.0.0.1:8810/8811, as npx cseries's QEMU forwards publish them. No provider key anywhere.
set -euo pipefail
TREE=$1; W=$2; BAST=$3
log() { printf '%s run %s\n' "$(date -u +%H:%M:%S.%3NZ)" "$*"; }
mount --make-rprivate /
ip link set lo up
# All of /run (so /var/run/privatae-bastion's ask and context sockets, /run/privatae's lock, the
# system bus) and /tmp are this namespace's own; /etc is an overlay whose writes stay here.
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
# The image's /app layout, from this tree (Dockerfile.linux COPY lines), read-only.
mount -t tmpfs tmpfs /app
for d in server brain dashboard cli native-bundles native-sdk default-skills migrations docs scripts system-apps; do
  mkdir -p "/app/$d" && mount --bind "$TREE/$d" "/app/$d" && mount -o remount,bind,ro "/app/$d"
done
mkdir -p /app/designs/builtin && mount --bind "$TREE/designs/builtin" /app/designs/builtin && mount -o remount,bind,ro /app/designs/builtin
cp "$TREE/init-db.sql" /app/init-db.sql
mkdir -p /app/system/apps /app/workspace /app/projects /app/ceigas /app/portal /app/user-files /app/data /app/secrets /app/integrations /app/engine /app/training_memory_jobs

log "the Host's VM: a network namespace of its own, 10.77.0.2, reached from the machine at 10.77.0.1"
ip netns add vm
ip link add m0 type veth peer name eth0 netns vm
ip addr add 10.77.0.1/30 dev m0 && ip link set m0 up
ip -n vm link set lo up && ip -n vm addr add 10.77.0.2/30 dev eth0 && ip -n vm link set eth0 up
ip -br addr; ip -n vm -br addr

log "stand-in OpenAI-compatible server on the machine's 127.0.0.1:8080"
python3 -I "$W/standin_openai.py" 127.0.0.1 8080 >"$W/standin.log" 2>&1 &
for _ in $(seq 50); do curl -s 127.0.0.1:8080/v1/models >/dev/null && break; sleep 0.1; done
if ip netns exec vm curl -s -m 3 http://10.77.0.1:8080/v1/models >/dev/null 2>&1 || ip netns exec vm curl -s -m 3 http://127.0.0.1:8080/v1/models >/dev/null 2>&1; then
  log "FAIL: the Host's VM can reach the model server directly"; exit 1
fi
log "ok: from the Host's VM the model server is unreachable (10.77.0.1:8080 and its own 127.0.0.1:8080)"
env | grep -E '^(OPENROUTER|ANTHROPIC|OPENAI|PRIVATAE_LLM|CSERIES_LLM|OLLAMA)' || log "no provider key and no model address in the environment"

log "the C-Series Host (python3 -m server.launch, this branch) in the VM namespace"
mkdir -p "$W/hostdir" "$W/host-home"
HOSTENV=(env -i PATH=/usr/local/bin:/usr/bin:/bin HOME="$W/host-home" PYTHONPATH="/app:/app/brain"
  DATABASE_URL="$(cat "$W/DSN")" ENTITY_ID=1 PRIVATAE_HOST_DIR="$W/hostdir" TM_JOBS_ROOT="$W/tm-jobs")
( cd /app && exec ip netns exec vm "${HOSTENV[@]}" DASHBOARD_BIND_HOST=127.0.0.1 DASHBOARD_PORT=8800 \
    "$W/venv/bin/python" -m server.launch ) >"$W/host.log" 2>&1 &
for _ in $(seq 300); do (ip netns exec vm ss -ltn | grep -q ':8811 ') && break; sleep 0.5; done
ip netns exec vm ss -ltn | grep -E ':88(00|10|11) ' || { log "FAIL: the Host's ports did not come up"; tail -50 "$W/host.log"; exit 1; }
log "the machine's 127.0.0.1:8810/8811 forward to the Host's enroll and lane ports (as QEMU's hostfwd)"
socat TCP-LISTEN:8810,bind=127.0.0.1,reuseaddr,fork TCP:10.77.0.2:8810 &
socat TCP-LISTEN:8811,bind=127.0.0.1,reuseaddr,fork TCP:10.77.0.2:8811 &
sleep 0.5

log "pair, as npx cseries does: pair-token in the Host, then pair --redeem-url <enroll> --host-ca <ca> - on the machine"
( cd /app && ip netns exec vm "${HOSTENV[@]}" "$W/venv/bin/python" -m cli.cseries bastion pair-token ) >"$W/pair-token.json" 2>"$W/pair-token.err" || { cat "$W/pair-token.err"; exit 1; }
python3 -I -c 'import json,sys;j=json.load(open(sys.argv[1]));open(sys.argv[2],"w").write(j["host_ca_pem"].strip()+"\n");open(sys.argv[3],"w").write(j["token"]);print("  enroll:",j["enroll"])' \
    "$W/pair-token.json" "$W/host-ca.pem" "$W/token"
ENROLL=$(python3 -I -c 'import json,sys;print(json.load(open(sys.argv[1]))["enroll"])' "$W/pair-token.json")
export HOME="$W/bastion-home" XDG_CONFIG_HOME="$W/bastion-home/.config"
mkdir -p "$XDG_CONFIG_HOME"
unset PRIVATAE_LLM_ADDR OLLAMA_ADDR
"$BAST" pair --redeem-url "$ENROLL" --host-ca "$W/host-ca.pem" - <"$W/token" 2>&1 | sed -n '1,4p'
log "the bastion runs"
"$BAST" run >"$W/bastion.log" 2>&1 &
for _ in $(seq 120); do grep -q "found an OpenAI-compatible" "$W/bastion.log" && break; sleep 0.5; done
grep -E "serve workers up|found an OpenAI" "$W/bastion.log" | head -3
touch "$W/READY"
log "ready; waiting for $W/DONE"
while [ ! -e "$W/DONE" ]; do sleep 1; done
log "stopped"
