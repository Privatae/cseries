#!/usr/bin/env bash
# Boots the WSL guest image under systemd in a privileged container (WSL
# itself cannot run here) and runs the Host in it through the guest's own
# units and the cseries command's own guest steps. Removes everything it made
# when it ends.
#
#   vm/wsl/test/boot-test.sh <cseries-wsl-rootfs-*.tar.gz> [host image] [postgres image]
#
# The two images (default cseries:dev and cseries-postgres:dev) are loaded
# from `docker save` tarballs, since nothing is published yet, and referred to
# by digest, as a release does. Needs Docker, the compose plugin (DOCKER_CONFIG
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

rootfs=$(readlink -f "${1:?usage: $0 <rootfs.tar.gz> [host image] [postgres image]}")
host_img=${2:-cseries:dev}
pg_img=${3:-cseries-postgres:dev}
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
ask='import socket; s = socket.socket(socket.AF_UNIX); s.connect("/run/cseries-runtime/runtime.sock"); s.sendall(b"{\"op\": \"runtime.status\"}\n"); print(s.makefile().readline().strip())'
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
rc=0
timeout 120 podman exec cseries-host python3 -c "b = b\"x\" * (3 * 1024**3)" || rc=$?
kills=$(sed -n "s/^oom_kill //p" $cg/cseries-host.service/memory.events)
ok=0
healthy && ok=1
rm -f "$d"
systemctl daemon-reload
systemctl restart cseries-host.service
[ "$rc" = 137 ] || { echo "FAIL: a 3 GB runaway under a 1500 MB ceiling was not stopped (exit $rc)"; exit 1; }
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

say "PASS"
