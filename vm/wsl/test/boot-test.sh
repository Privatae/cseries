#!/usr/bin/env bash
# Boots the WSL guest image under systemd in a privileged container (WSL
# itself cannot run here) and runs the Host in it through the guest's own
# units and the cseries command's own guest steps. Removes everything it made
# when it ends.
#
#   vm/wsl/test/boot-test.sh <cseries-wsl-rootfs-*.tar.gz> [host image] [postgres image] [door image]
#
# The images (default cseries:dev, cseries-postgres:dev and
# cseries-remote-door:dev, built from sidecars/remote-door/Dockerfile) are
# loaded from `docker save` tarballs, since nothing is published yet, and
# referred to by digest, as a release does. The guest runs both ways a
# release can configure it: first with the Host and its database alone, then
# with the door the release pins, given through the cseries command's own
# steps, which joins the Host to the remote network (cseries-remote.network)
# beside the door; that part ends with a restart, from which the Host and the
# door must both come back. Needs Docker, the compose plugin (DOCKER_CONFIG
# may point at it) and Node 18 or later on this machine. WORK sets the scratch
# directory (default /var/tmp); KEEP=1 leaves the guest container running for
# a look inside after a failure (remove it with docker rm -f).
#
# With RT_PY_IMAGE and RT_BROWSER_IMAGE (the runtime's python and browser
# images, built from this tree) it also starts the local runtime's containers
# in the guest through the Host's own code: the Telegram connector installed
# from its signed bundle and woken as its window wakes it, the browser woken
# as the Host's browser lane wakes it, and the connector back by itself after
# the guest restarts. Without them that part is skipped, and says so.
set -euo pipefail

rootfs=$(readlink -f "${1:?usage: $0 <rootfs.tar.gz> [host image] [postgres image] [door image]}")
host_img=${2:-cseries:dev}
pg_img=${3:-cseries-postgres:dev}
door_img=${4:-cseries-remote-door:dev}
rt_py=${RT_PY_IMAGE:-}
rt_browser=${RT_BROWSER_IMAGE:-}
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../../.." && pwd)
name="cseries-npxtest-$$"
work=$(mktemp -d "${WORK:-/var/tmp}/$name.XXXXXX")
node_bin=$(readlink -f "$(command -v node)")

cleanup() {
    if [ "${KEEP:-}" = 1 ]; then
        echo "KEEP=1: left $name running; docker rm -f $name; docker volume rm $name-storage; docker rmi $name-rootfs:local; rm -rf $work"
        return
    fi
    docker rm -f "$name" >/dev/null 2>&1 || true
    docker volume rm "$name-storage" >/dev/null 2>&1 || true
    docker rmi "$name-rootfs:local" >/dev/null 2>&1 || true
    rm -rf "$work"
}
trap cleanup EXIT

# Every command into the guest leaves the root cgroup first, as cseries-guest
# does for itself (see leave_root_cgroup there): a docker exec that lands in
# the container's root cgroup stops systemd from enabling controllers.
G() {
    docker exec -i "$name" sh -c 'grep -qx "0::/" /proc/self/cgroup && echo $$ > /sys/fs/cgroup/init.scope/cgroup.procs 2>/dev/null; exec "$@"' sh "$@"
}
say() { printf '\n== %s\n' "$*"; }

say "guest image $(basename "$rootfs") ($(sha256sum "$rootfs" | cut -c1-16)...)"
docker import "$rootfs" "$name-rootfs:local" >/dev/null
docker save "$host_img" "$pg_img" -o "$work/images.tar"
# The door's image goes into the guest only when the second part configures
# it, as an install pulls it then.
docker save "$door_img" -o "$work/door.tar"
if [ -n "$rt_py" ] && [ -n "$rt_browser" ]; then
    docker save "$rt_py" "$rt_browser" -o "$work/runtime-images.tar"
    cp "$repo/vm/cloud/test/runtime_helper.py" "$repo/factory-bundles/telegram.cseries" "$work/"
fi
docker volume create "$name-storage" >/dev/null
docker run -d --name "$name" --privileged --cgroupns=private \
    --tmpfs /run --tmpfs /run/lock --tmpfs /tmp \
    -v "$name-storage:/var/lib/containers" \
    -v "$work:/test:ro" \
    -v "$repo/packages/cseries:/opt/cseries-cli:ro" \
    -v "$here:/opt/cseries-test:ro" \
    -v "$node_bin:/usr/local/bin/node:ro" \
    "$name-rootfs:local" /sbin/init >/dev/null

say "boot under systemd"
for _ in $(seq 1 30); do G true 2>/dev/null && break; sleep 1; done
G cseries-guest wait-boot
G systemctl --failed --no-legend --plain || true
G sh -c 'echo "guest $(cseries-guest version), $(podman --version), $(systemctl --version | head -1)"'

say "load the two images and pin them by digest"
G podman load -q -i /test/images.tar
host_ref="localhost/${host_img}@$(G podman image inspect --format '{{.Digest}}' "localhost/$host_img")"
pg_ref="localhost/${pg_img}@$(G podman image inspect --format '{{.Digest}}' "localhost/$pg_img")"
echo "host:     $host_ref"
echo "postgres: $pg_ref"

say "a reference without a digest is refused"
if printf 'host=localhost/%s\npostgres=%s\nrelease=1\n' "$host_img" "$pg_ref" | G cseries-guest configure 2>&1; then
    echo "FAIL: configure took an unpinned image"; exit 1
fi

say "the cseries command's guest steps, twice"
G node /opt/cseries-test/e2e-guest.js /opt/cseries-cli "$host_ref" "$pg_ref"

say "the local runtime: its socket, proxy and firewall, and the Host on its network"
G stat -c '%n %a %U:%G' /run/cseries-runtime /run/cseries-runtime/runtime.sock
[ "$(G stat -c '%a %U:%G' /run/cseries-runtime)" = "750 root:cseries-runtime" ] ||
    { echo "FAIL: the runtime's socket directory is not 0750 root:cseries-runtime"; exit 1; }
G systemctl is-active cseries-runtime.socket cseries-squid.service cseries-runtime-firewall.service ||
    { echo "FAIL: a runtime unit is not active"; G systemctl status --no-pager cseries-squid.service || true; exit 1; }
G nft list table bridge cseries_apps >/dev/null || { echo "FAIL: the runtime's bridge filter is not loaded"; exit 1; }
# The runtime answers a peer it refuses without reading its request, and
# closes: a client slower than that (a loaded machine) finds the pipe broken
# when it sends, and the refusal is still there to read. So a broken pipe on
# the send is not the answer; the line read after it is.
ask='import socket
s = socket.socket(socket.AF_UNIX); s.connect("/run/cseries-runtime/runtime.sock")
try:
    s.sendall(b"{\"op\": \"runtime.status\"}\n")
except BrokenPipeError:
    pass
print(s.makefile().readline().strip())'
st=$(G podman exec cseries-host python3 -c "$ask")
echo "from the Host: ${st:0:160}"
grep -q '"ok": true' <<< "$st" || { echo "FAIL: the runtime does not answer the Host"; G journalctl -u cseries-runtime --no-pager -n 30 || true; exit 1; }
# One memory budget: the runtime leaves the Host's and the database's own
# ceilings (their units' MemoryMax, as systemd holds them) out of what it
# admits containers into.
held=$(python3 -c 'import json, sys; print(json.loads(sys.argv[1])["data"]["held_mi"])' "$st")
hmax=$(G systemctl show -p MemoryMax --value cseries-host.service)
pmax=$(G systemctl show -p MemoryMax --value cseries-postgres.service)
echo "the runtime holds back ${held} MiB: the Host's MemoryMax $hmax and the database's $pmax"
[ "$held" = $(( (hmax + 1048575) / 1048576 + (pmax + 1048575) / 1048576 )) ] ||
    { echo "FAIL: the runtime's held_mi $held is not the Host's and the database's ceilings"; exit 1; }
st=$(G python3 -c "$ask" 2>&1 || true)
grep -q 'only the Host may use this socket' <<< "$st" || { echo "FAIL: root in the guest was not refused: $st"; exit 1; }
st=$(G runuser -u cseries -- python3 -c "$ask" 2>&1 || true)
grep -q 'PermissionError' <<< "$st" || { echo "FAIL: the guest's own user reached the socket: $st"; exit 1; }
nets=$(G podman inspect cseries-host --format '{{range $k, $v := .NetworkSettings.Networks}}{{$k}}={{$v.IPAddress}} {{end}}')
echo "the Host's networks: $nets"
grep -q 'cseries-apps=10.89.78.2' <<< "$nets" || { echo "FAIL: the Host is not on the runtime's network"; exit 1; }
# With no door configured the Host names no remote network, and nothing has
# made it (cseries-remote.network: only a unit that names it starts it).
if grep -q 'cseries_remote' <<< "$nets"; then echo "FAIL: the Host joined the remote network with no door configured"; exit 1; fi
if G podman network exists cseries_remote; then echo "FAIL: the remote network was made with no door configured"; exit 1; fi
G sh -c 'test -s /etc/cseries-runtime/browser-control-token && stat -c "%a" /etc/cseries-runtime/browser-control-token' | grep -qx 600 ||
    { echo "FAIL: the browser's control token is missing or not 0600"; exit 1; }

# The Host's own code, in a process of its own in the Host container (the
# cloud VM test's helper: vm/cloud/test/runtime_helper.py).
H() {
    G podman cp /test/runtime_helper.py cseries-host:/tmp/runtime_helper.py
    G podman exec cseries-host python3 /tmp/runtime_helper.py "$@"
}
jq_py() { python3 -c 'import json, sys; d = json.loads(sys.argv[1]); print(eval(sys.argv[2]))' "$1" "$2"; }
if [ -n "$rt_py" ] && [ -n "$rt_browser" ]; then
    say "the local runtime's containers: Telegram from its signed bundle, and the browser"
    G podman load -q -i /test/runtime-images.tar
    ref() { echo "localhost/${1%:*}@$(G podman image inspect --format '{{.Digest}}' "localhost/$1")"; }
    rel=$(G cseries-guest status | sed -n 's/^release=//p')
    printf 'host=%s\npostgres=%s\nrelease=%s\npython=%s\nbrowser=%s\n' \
        "$host_ref" "$pg_ref" "$rel" "$(ref "$rt_py")" "$(ref "$rt_browser")" | G cseries-guest configure
    G cseries-guest restart
    for _ in $(seq 1 100); do G cseries-guest ready >/dev/null 2>&1 && break; sleep 3; done
    G cseries-guest ready || { echo "FAIL: the Host is not ready after the runtime's images were pinned"; exit 1; }
    G podman cp /test/telegram.cseries cseries-host:/tmp/telegram.cseries
    r=$(H install-file /tmp/telegram.cseries)
    echo "$r"
    [ "$(jq_py "$r" 'd["installed"]')" = telegram ] || { echo "FAIL: the signed Telegram bundle did not install"; exit 1; }
    w=$(H wake-app telegram)
    echo "$w"
    up=$(jq_py "$w" 'd["upstream"] or ""')
    [ -n "$up" ] || { echo "FAIL: the connector's container did not wake"; G journalctl -u cseries-runtime --no-pager -n 40 || true; exit 1; }
    h=$(H http "http://$up/health")
    echo "$h"
    [ "$(jq_py "$h" 'd["status"] == 200 and json.loads(d["body"])["ok"] is True and "waiting for the pairing" in json.loads(d["body"])["phase"]')" = True ] ||
        { echo "FAIL: the connector does not answer its health from inside the runtime"; exit 1; }
    insp=$(G podman container inspect cseries-app-telegram --format '{{.HostConfig.ReadonlyRootfs}} {{.HostConfig.Memory}} {{.HostConfig.MemorySwap}} {{.HostConfig.LogConfig.Type}}')
    um=$(G sh -c 'cat /proc/$(podman container inspect --format {{.State.Pid}} cseries-app-telegram)/uid_map')
    echo "telegram's container: $insp; uid map: $um"
    python3 -c 'import sys; f = sys.argv[1].split(); u = sys.argv[2].split()
sys.exit(0 if f[0] == "true" and f[1] == f[2] and int(f[1]) > 0 and f[3] == "journald"
         and u[0] == "0" and int(u[1]) >= 200000 and u[2] == "65536" else 1)' "$insp" "$um" ||
        { echo "FAIL: the connector's container is not read-only with no swap, logging to the journal, in a user range of its own"; exit 1; }
    b=$(H wake-browser)
    echo "$b"
    [ "$(jq_py "$b" 'str(d["upstream"]).endswith(":8830")')" = True ] || { echo "FAIL: the browser did not wake"; exit 1; }
    h=$(H browser GET /health)
    echo "browser: ${h:0:200}"
    [ "$(jq_py "$h" 'd["status"] == 200 and d["body"]["ok"] is True')" = True ] ||
        { echo "FAIL: Chromium does not answer its health inside the runtime's user namespace"; exit 1; }
    f=$(H browser-frame)
    echo "viewport: $f"
    [ "$(jq_py "$f" 'd["frame"] is True')" = True ] || { echo "FAIL: the browser's viewport WebSocket delivered no frame"; exit 1; }
    H release harness browser >/dev/null
    if G podman container exists cseries-h-browser; then echo "FAIL: a release left the browser's container"; exit 1; fi
    echo "the connector and the browser ran in the guest through the runtime"
else
    say "the local runtime's containers: SKIPPED (set RT_PY_IMAGE and RT_BROWSER_IMAGE)"
fi

say "the machine wall: no container in the guest reaches the machine"
# Here the machine is this test's host, at the guest container's gateway, as
# WSL's NAT puts the Windows machine at the guest's gateway. A listener of
# this test's on that address only, on a port the kernel picks; this host's
# own firewall may keep the guest off it, so the guest's own reach is told,
# not required, and the containers' side is checked either way.
gw=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.Gateway}}{{end}}' "$name")
port_file="$work/wall-port"
python3 -I -c '
import socket, sys, time
s = socket.socket(); s.bind((sys.argv[1], 0)); s.listen(8)
open(sys.argv[2], "w").write(str(s.getsockname()[1]))
s.settimeout(1)
end = time.time() + 300
while time.time() < end:
    try:
        s.accept()[0].close()
    except OSError:
        pass
' "$gw" "$port_file" &
wall_listener=$!
for _ in $(seq 1 20); do [ -s "$port_file" ] && break; sleep 0.2; done
G env MACHINE_PATH_OPTIONAL=1 bash -s -- "${host_ref%@*}" "$gw" "$(cat "$port_file")" < "$here/machine-wall-checks.sh"
kill "$wall_listener" 2>/dev/null || true

say "the hardening, against compose.yaml"
G podman inspect cseries-postgres cseries-host > "$work/inspect.json"
(cd "$repo" && docker compose -f compose.yaml config --format json) > "$work/compose.json"
repo_seccomp=$(sha256sum "$repo/seccomp-personal.json" | cut -d' ' -f1)
guest_seccomp=$(G sha256sum /usr/share/cseries/seccomp-personal.json | cut -d' ' -f1)
for c in cseries-postgres cseries-host; do
    G podman exec "$c" cat /proc/1/status > "$work/status-$c"
    G systemctl show "$c.service" -p Restart --value > "$work/restart-$c"
done
node -e '
const fs = require("fs"); const [w, g, r] = process.argv.slice(1); const out = {};
for (const c of ["cseries-postgres", "cseries-host"]) {
  out[c] = { status: fs.readFileSync(`${w}/status-${c}`, "utf8"), unitRestart: fs.readFileSync(`${w}/restart-${c}`, "utf8").trim(),
             seccompSha256: g, repoSeccompSha256: r };
}
fs.writeFileSync(`${w}/runtime.json`, JSON.stringify(out));
' "$work" "$guest_seccomp" "$repo_seccomp"
node "$here/compare-hardening.js" "$work/compose.json" "$work/inspect.json" "$work/runtime.json"

say "the resource limits, as systemd and the kernel hold them"
# The Host: the ceiling configure wrote to 10-memory.conf from the MemTotal
# `status` reports (e2e-guest.js checks its value against host.js), no swap
# (cseries-host.container), and every processor but one (10-cpu.conf, which
# configure wrote from the count `status` reports). The database: 512 MB, no
# swap, no processor limit.
G sh -c '
set -eu
cg=/sys/fs/cgroup/system.slice
prop() { systemctl show "$1" -p "$2" --value; }
cpus=$(getconf _NPROCESSORS_ONLN)
quota=$(( (cpus > 1 ? cpus - 1 : 1) * 100000 ))
mem=$(( $(sed -n "s/^MemTotal: *\([0-9]*\) kB$/\1/p" /proc/meminfo) * 1024 ))
hmax=$(prop cseries-host.service MemoryMax)
echo "processors $cpus, memory $mem; Host MemoryMax $hmax CPUQuota $(prop cseries-host.service CPUQuotaPerSecUSec)"
echo "cgroup: Host memory.max $(cat $cg/cseries-host.service/memory.max) memory.swap.max $(cat $cg/cseries-host.service/memory.swap.max) cpu.max $(cat $cg/cseries-host.service/cpu.max); database memory.max $(cat $cg/cseries-postgres.service/memory.max) memory.swap.max $(cat $cg/cseries-postgres.service/memory.swap.max) cpu.max $(cat $cg/cseries-postgres.service/cpu.max)"
near() { [ $(( $1 > $2 ? $1 - $2 : $2 - $1 )) -le 4096 ]; }
want=$(sed -n "s/^MemoryMax=\([0-9]*\)M$/\1/p" /etc/containers/systemd/cseries-host.container.d/10-memory.conf)
[ -n "$want" ] || { echo "FAIL: configure wrote no 10-memory.conf"; exit 1; }
[ "$hmax" = $(( want * 1048576 )) ] || { echo "FAIL: Host MemoryMax $hmax is not the ${want}M in 10-memory.conf"; exit 1; }
near "$(cat $cg/cseries-host.service/memory.max)" "$hmax" || { echo "FAIL: the kernel does not hold the Host'"'"'s MemoryMax"; exit 1; }
[ "$(cat $cg/cseries-host.service/memory.high)" = max ] || { echo "FAIL: the Host has a memory.high"; exit 1; }
[ "$(cat $cg/cseries-host.service/memory.swap.max)" = 0 ] || { echo "FAIL: the Host may swap"; exit 1; }
[ "$(cat $cg/cseries-host.service/cpu.max)" = "$quota 100000" ] || { echo "FAIL: Host cpu.max is not $quota 100000"; exit 1; }
[ "$(cat $cg/cseries-postgres.service/memory.max)" = 536870912 ] || { echo "FAIL: database memory.max is not 512 MB"; exit 1; }
[ "$(cat $cg/cseries-postgres.service/memory.swap.max)" = 0 ] || { echo "FAIL: the database may swap"; exit 1; }
[ "$(cat $cg/cseries-postgres.service/cpu.max)" = "max 100000" ] || { echo "FAIL: the database has a processor limit"; exit 1; }
# A drop-in that sorts after the shipped ones replaces every limit, the way
# a machine-sized drop-in does; taking it away puts them back. systemd writes
# the limits in a unit file into the cgroup when the unit starts, so each
# change is a reload and a restart.
d=/etc/containers/systemd/cseries-host.container.d/zz-boot-test.conf
printf "[Service]\nMemoryMax=3G\nMemorySwapMax=1G\nCPUQuota=150%%\n" > "$d"
systemctl daemon-reload
systemctl restart cseries-host.service
got="$(cat $cg/cseries-host.service/memory.max) $(cat $cg/cseries-host.service/memory.swap.max) $(cat $cg/cseries-host.service/cpu.max)"
rm -f "$d"
systemctl daemon-reload
systemctl restart cseries-host.service
[ "$got" = "3221225472 1073741824 150000 100000" ] || { echo "FAIL: the drop-in did not replace the limits: $got"; exit 1; }
[ "$(cat $cg/cseries-host.service/memory.swap.max)" = 0 ] || { echo "FAIL: no swap did not come back without the drop-in"; exit 1; }
[ "$(cat $cg/cseries-host.service/cpu.max)" = "$quota 100000" ] || { echo "FAIL: the limits did not come back without the drop-in"; exit 1; }
near "$(cat $cg/cseries-host.service/memory.max)" "$hmax" || { echo "FAIL: the memory ceiling did not come back without the drop-in"; exit 1; }
echo "a later drop-in replaced all three ($got) and its removal restored them"
# A process in the Host that takes more than the Host may have is stopped,
# and the Host answers after it (it goes on, or systemd brings it back).
healthy() {
    for _ in $(seq 1 60); do
        curl -fsS --max-time 3 -o /dev/null http://127.0.0.1:8800/api/system/health && return 0
        sleep 3
    done
    return 1
}
printf "[Service]\nMemoryMax=1500M\n" > "$d"
systemctl daemon-reload
systemctl restart cseries-host.service
healthy || { echo "FAIL: the Host did not start under a 1500 MB ceiling"; exit 1; }
# The runaway makes itself the OOM killer'"'"'s first choice (oom_score_adj
# 1000, which any process may raise for itself): while it is still growing the
# Host'"'"'s own server can be the biggest process in the unit, and a kill of the
# server instead (a loaded machine) took the container and the runaway with
# it (exit 255). This measures the ceiling, not which process the kernel picks.
rc=0
k0=$(sed -n "s/^oom_kill //p" $cg/cseries-host.service/memory.events)
timeout 120 podman exec cseries-host python3 -c "import os; fd = os.open(\"/proc/self/oom_score_adj\", os.O_WRONLY); os.write(fd, b\"1000\"); os.close(fd); b = b\"x\" * (3 * 1024**3)" || rc=$?
kills=$(sed -n "s/^oom_kill //p" $cg/cseries-host.service/memory.events)
ok=0
healthy && ok=1
rm -f "$d"
systemctl daemon-reload
systemctl restart cseries-host.service
[ "$rc" = 137 ] || { echo "FAIL: a 3 GB runaway under a 1500 MB ceiling was not stopped (exit $rc)"; exit 1; }
[ "${kills:-0}" -gt "${k0:-0}" ] || { echo "FAIL: the runaway was not stopped by the Host'"'"'s own ceiling (oom_kill ${k0:-?} -> ${kills:-?})"; exit 1; }
[ "$ok" = 1 ] || { echo "FAIL: the Host did not answer after the runaway"; exit 1; }
echo "a 3 GB runaway under a 1500 MB ceiling was stopped (exit 137, ${kills:-?} OOM kill(s) in the Host) and the Host answered after it"
'

say "inside the containers"
for c in cseries-postgres cseries-host; do
    echo "-- $c: $(G podman exec "$c" id)"
    G podman exec "$c" sh -c 'touch /probe 2>&1 | head -1; echo "pids.max $(cat /sys/fs/cgroup/pids.max)"'
done

say "what WSL's forwarding sees: listeners on the guest's loopback only"
G ss -ltnH | awk '{print $4}' | grep -E ':(8800|8810|8811)$' | sort
if G ss -ltnH | awk '{print $4}' | grep -E ':(8800|8810|8811)$' | grep -vq '^127\.0\.0\.1:'; then
    echo "FAIL: a port is published beyond 127.0.0.1"; exit 1
fi

say "the VM restarts: the Host comes back by itself"
docker restart "$name" >/dev/null
for _ in $(seq 1 30); do G true 2>/dev/null && break; sleep 1; done
G cseries-guest wait-boot
ok=""
for _ in $(seq 1 60); do
    if G cseries-guest health 2>/dev/null; then ok=1; break; fi
    sleep 3
done
[ -n "$ok" ] || { echo "FAIL: the Host did not come back after a restart"; G cseries-guest logs host --lines 40; exit 1; }
G cseries-guest status
G curl -s -o /dev/null -w 'GET /setup -> %{http_code}\n' http://127.0.0.1:8800/setup
# A program on the machine reaching the Host through WSL's localhost forward, with no
# owner session: the Host must see a peer that is not its own loopback and
# send it to sign in (server/middleware/localhost_only.py). Were WSL's localhost forward
# to arrive as 127.0.0.1, every program on the machine would have the owner's API.
status=$(G curl -s -w ' %{http_code}' http://127.0.0.1:8800/api/system/status)
case "$status" in
    *'sign in first'*' 403') echo "an API call from the machine with no session is sent to sign in" ;;
    *) echo "FAIL: an API call from the machine with no session got: $status"; exit 1 ;;
esac
if [ -n "$rt_py" ] && [ -n "$rt_browser" ]; then
    back=""
    for _ in $(seq 1 60); do
        [ "$(G podman container inspect --format '{{.State.Running}}' cseries-app-telegram 2>/dev/null)" = true ] && { back=1; break; }
        sleep 3
    done
    [ -n "$back" ] || { echo "FAIL: the Host did not bring the Telegram connector back after the restart"; exit 1; }
    echo "the Host brought the Telegram connector back by itself (a resident app)"
fi

say "keepalive: a second one exits at once"
G sh -c 'cseries-guest keepalive & first=$!; sleep 1
    if timeout 5 cseries-guest keepalive; then echo "FAIL: a second holder started"; exit 1; fi
    echo "second holder exited at once"; kill "$first" 2>/dev/null || true'

# The second way: the release's door, as every install from the release that
# pins one gets it. The cseries command's own steps send it (host.js sends
# door= to a guest whose status names the door), configure writes the door's
# two units their image and the Host its remote.conf drop-in, and the Host
# restarts onto the remote network (cseries-remote.network, 10.89.79.0/29).
# A wrong network or drop-in stops the Host from starting here, as it would
# on every install.
say "remote access: the release's door, through the cseries command's guest steps"
# By the name podman gives it: an archive of one image that Docker's
# containerd store wrote loads under its full name (docker.io/library/...),
# not under localhost/ as the two images above do.
door_loaded=$(G podman load -q -i /test/door.tar | sed -n 's/^Loaded image: //p' | head -1)
[ -n "$door_loaded" ] || { echo "FAIL: podman loaded no door image from $door_img"; exit 1; }
door_ref="${door_loaded}@$(G podman image inspect --format '{{.Digest}}' "$door_loaded")"
echo "door:     $door_ref"
G node /opt/cseries-test/e2e-guest.js /opt/cseries-cli "$host_ref" "$pg_ref" "$door_ref"

# The Host and the door, each at its own address on the remote network and
# nowhere else for the door; the network as cseries-remote.network makes it;
# the Host's dashboard on the guest's 127.0.0.1:8800 as before; and nothing
# failed.
door_checks() {
    local st nets dnets net probe got
    for _ in $(seq 1 100); do G cseries-guest ready >/dev/null 2>&1 && break; sleep 3; done
    G cseries-guest ready || { echo "FAIL: the Host is not ready with the door configured ($1)"; G cseries-guest logs host --lines 40 || true; exit 1; }
    for _ in $(seq 1 60); do
        st=$(G cseries-guest status)
        grep -qx 'door=active' <<< "$st" && grep -qx 'remote_cert=active' <<< "$st" && break
        sleep 2
    done
    echo "$st"
    for want in host=active health=ok "door_image=$door_ref" door=active remote_cert=active; do
        grep -qxF "$want" <<< "$st" || {
            echo "FAIL: with the door configured ($1) the guest's status has no $want"
            G journalctl -u cseries-remote-door.service -u cseries-remote-cert.service -u cseries-remote-network.service --no-pager -n 40 || true
            exit 1; }
    done
    nets=$(G podman inspect cseries-host --format '{{range $k, $v := .NetworkSettings.Networks}}{{$k}}={{$v.IPAddress}} {{end}}')
    echo "the Host's networks: $nets"
    grep -qw 'cseries_remote=10\.89\.79\.2' <<< "$nets" || { echo "FAIL: the Host is not on the remote network at 10.89.79.2 ($1)"; exit 1; }
    grep -qw 'cseries_default=[0-9.]*' <<< "$nets" || { echo "FAIL: the Host left its database's network ($1)"; exit 1; }
    dnets=$(G podman inspect cseries-remote-door --format '{{.State.Status}} {{range $k, $v := .NetworkSettings.Networks}}{{$k}}={{$v.IPAddress}} {{end}}')
    echo "the door: $dnets"
    [ "$dnets" = "running cseries_remote=10.89.79.3 " ] ||
        { echo "FAIL: the door is not running on the remote network alone at 10.89.79.3 ($1)"; exit 1; }
    net=$(G podman network inspect cseries_remote --format '{{range .Subnets}}{{.Subnet}} {{end}}internal={{.Internal}} dns={{.DNSEnabled}}')
    echo "the remote network: $net"
    [ "$net" = "10.89.79.0/29 internal=true dns=false" ] ||
        { echo "FAIL: the remote network is not cseries-remote.network's internal 10.89.79.0/29 with no resolver ($1)"; exit 1; }
    # Across it: the door reaches the Host's web server at the Host's address
    # there, and nothing beyond the network (no route out); the Host reaches
    # the door's address (refused until a certificate lets Caddy start).
    probe='
import socket, sys
host, port = sys.argv[1], int(sys.argv[2])
try:
    s = socket.create_connection((host, port), 3)
except socket.timeout:
    print("dropped"); sys.exit()
except ConnectionRefusedError:
    print("refused"); sys.exit()
except OSError as e:
    print("error:%s" % e.errno); sys.exit()
if len(sys.argv) > 3:
    s.sendall(b"GET /api/system/health HTTP/1.1\r\nHost: " + sys.argv[1].encode() + b"\r\nConnection: close\r\n\r\n")
    s.settimeout(5)
    print(s.makefile("rb").readline().decode(errors="replace").split(" ")[0] or "no answer")
else:
    print("open")
'
    got=$(G podman exec cseries-remote-door python3 -I -c "$probe" 10.89.79.2 8800 http </dev/null)
    [ "$got" = HTTP/1.1 ] || { echo "FAIL: the door does not reach the Host's web server at 10.89.79.2:8800 ($1): $got"; exit 1; }
    got=$(G podman exec cseries-remote-door python3 -I -c "$probe" 1.1.1.1 443 </dev/null)
    [ "$got" = error:101 ] || { echo "FAIL: the door has a way out of the remote network ($1): 1.1.1.1:443 gave $got"; exit 1; }
    got=$(G podman exec cseries-host python3 -I -c "$probe" 10.89.79.3 8443 </dev/null)
    case "$got" in refused|open) ;; *) echo "FAIL: the Host does not reach the door's address 10.89.79.3:8443 ($1): $got"; exit 1 ;; esac
    echo "the door reaches the Host at 10.89.79.2:8800 and nothing out; the Host reaches the door at 10.89.79.3 ($got: no certificate yet)"
    got=$(G curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8800/setup)
    [ "$got" = 200 ] || { echo "FAIL: the Host's dashboard does not answer on the guest's 127.0.0.1:8800 ($1): GET /setup -> $got"; exit 1; }
    echo "GET http://127.0.0.1:8800/setup -> 200"
    if G ss -ltnH | awk '{print $4}' | grep -E ':(8800|8810|8811)$' | grep -vq '^127\.0\.0\.1:'; then
        echo "FAIL: a port is published beyond 127.0.0.1 with the door ($1)"; exit 1
    fi
    if G ss -ltnH | awk '{print $4}' | grep -q ':8443$'; then echo "FAIL: the door's port is published in the guest ($1)"; exit 1; fi
    for u in cseries-remote-door.service cseries-remote-cert.service; do
        [ "$(G systemctl show -p NRestarts --value "$u")" = 0 ] || { echo "FAIL: $u restarted ($1)"; G journalctl -u "$u" --no-pager -n 30 || true; exit 1; }
    done
    for c in cseries-host cseries-postgres; do
        G podman healthcheck run "$c" </dev/null || { echo "FAIL: $c is not healthy with the door configured ($1)"; exit 1; }
    done
    # The guest's own units. podman runs each container's healthcheck as a
    # transient unit named after the container's id; its first run, as the
    # container starts, fails (health "starting") and the unit stays listed
    # as failed until the next run 30 s later, which is also why wait-boot
    # says degraded after a restart. Those are told, not counted: the two
    # healthchecks above are run now.
    got=$(G systemctl --failed --no-legend --plain)
    if grep -q '^cseries' <<< "$got"; then echo "FAIL: a C-Series unit failed with the door configured ($1): $got"; exit 1; fi
    [ -z "$got" ] || echo "(failed, not C-Series units: $(awk '{print $1}' <<< "$got" | tr '\n' ' '))"
    echo "no C-Series unit failed; the Host and its database are healthy"
}
door_checks "after configure"

say "remote access: the VM restarts with the door, and both come back by themselves"
docker restart "$name" >/dev/null
for _ in $(seq 1 30); do G true 2>/dev/null && break; sleep 1; done
G cseries-guest wait-boot
door_checks "after the restart"

say "PASS"
