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
