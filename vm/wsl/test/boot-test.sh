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
set -euo pipefail

rootfs=$(readlink -f "${1:?usage: $0 <rootfs.tar.gz> [host image] [postgres image]}")
host_img=${2:-cseries:dev}
pg_img=${3:-cseries-postgres:dev}
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

say "keepalive: a second one exits at once"
G sh -c 'cseries-guest keepalive & first=$!; sleep 1
    if timeout 5 cseries-guest keepalive; then echo "FAIL: a second holder started"; exit 1; fi
    echo "second holder exited at once"; kill "$first" 2>/dev/null || true'

say "PASS"
