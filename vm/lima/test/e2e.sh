#!/usr/bin/env bash
# The Lima driver end to end on a Linux machine with KVM, in a privileged
# container that boots systemd and stands in for a fresh Debian desktop: a
# person's account with sudo, a logged-in user manager, /dev/kvm in its group
# with the person not in it, and no QEMU. Removes everything it made when it
# ends.
#
#   vm/lima/test/e2e.sh <guest.qcow2> <privatae-bastion linux-amd64> <host image> <postgres image>
#
# The guest image is vm/lima/build-image.sh's; the bastion is a build at
# least packages/cseries/release.json's bastion.min_version (e2e-lima.js
# reads both); the two images are local Docker images (nothing is published yet),
# handed to the guest as a `docker save` tarball. Needs Docker, /dev/kvm and
# Node 18 or later here. WORK sets the scratch folder (default /var/tmp),
# which needs about 12 GB free: the person's home, and the VM's disk in it,
# are there;
# KEEP=1 leaves the container for a look inside.
#
# Checked, in order: e2e-lima.js's two passes (the second asks for nothing);
# the Host answering on the machine's 127.0.0.1:8800 and nothing else of the
# guest's forwarded; the local runtime on the guest's real podman and systemd
# (vm/runtime/test/real-podman-checks.sh: the containers' CPU slice, an app's
# kept data exported and imported back); the bastion as the systemd service, paired and anchored;
# the VM started again by the login unit in a fresh user manager; and
# uninstall --delete-data.
set -euo pipefail

qcow2=$(readlink -f "${1:?usage: $0 <guest.qcow2> <bastion> <host image> <postgres image>}")
bastion=$(readlink -f "${2:?bastion binary}")
host_img=${3:?host image}
pg_img=${4:?postgres image}
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../../.." && pwd)
name="cseries-lima-e2e-$$"
work=$(mktemp -d "${WORK:-/var/tmp}/$name.XXXXXX")
node_bin=$(readlink -f "$(command -v node)")
[ -e /dev/kvm ] || { echo "no /dev/kvm here" >&2; exit 1; }

cleanup() {
    if [ "${KEEP:-}" = 1 ]; then
        echo "KEEP=1: left $name; docker rm -f $name; docker rmi $name-box:local; rm -rf $work"
        return
    fi
    docker rm -f "$name" >/dev/null 2>&1 || true
    docker rmi "$name-box:local" >/dev/null 2>&1 || true
    rm -rf "$work"
}
trap cleanup EXIT
say() { printf '\n== %s\n' "$*"; }
B() { docker exec "$name" "$@"; }
U() { docker exec -u tester -e HOME=/home/tester -e XDG_RUNTIME_DIR=/run/user/1500 \
      -e DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1500/bus -w /home/tester "$name" "$@"; }
# The same, with this script's standard input passed through.
Ui() { docker exec -i -u tester -e HOME=/home/tester -e XDG_RUNTIME_DIR=/run/user/1500 \
      -e DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1500/bus -w /home/tester "$name" "$@"; }

say "the machine"
cp "$qcow2" "$work/guest.qcow2"
cp "$bastion" "$work/privatae-bastion"
docker save "$host_img" "$pg_img" -o "$work/images.tar"
chmod -R a+rX "$work"
# The person's home, where Lima keeps the VM's disk, is a folder of $work
# rather than the container's own layer, so the run needs room only there.
mkdir -p "$work/home"
chown 1500:1500 "$work/home"
docker build -q -t "$name-box:local" - >/dev/null <<'EOF'
FROM debian:trixie-slim@sha256:a29215f6a35e51e22adffa17f89e9d2ef06214e64a2bad10d765c46aea49f11f
RUN apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
      systemd systemd-sysv dbus dbus-user-session libpam-systemd sudo ca-certificates procps iproute2 curl util-linux login passwd tar gzip >/dev/null \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --uid 1500 --create-home --shell /bin/bash tester \
 && echo 'tester ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/tester && chmod 440 /etc/sudoers.d/tester \
 && systemctl mask systemd-firstboot.service getty@tty1.service
STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]
EOF
docker run -d --name "$name" --privileged --cgroupns=private \
    --tmpfs /run --tmpfs /run/lock --tmpfs /tmp:exec \
    -v "$work:/e2e:ro" \
    -v "$work/home:/home/tester" \
    -v "$repo/packages/cseries:/opt/cseries-cli:ro" \
    -v "$here:/opt/e2e:ro" \
    -v "$node_bin:/usr/local/bin/node:ro" \
    "$name-box:local" >/dev/null
for _ in $(seq 1 30); do B systemctl is-system-running --wait >/dev/null 2>&1 && break; sleep 1; done
# /dev/kvm in its own group, as on a desktop distribution, the person not in it.
B sh -c "getent group kvm >/dev/null || groupadd -g $(stat -c %g /dev/kvm) kvm"
B loginctl enable-linger tester
for _ in $(seq 1 30); do B test -S /run/user/1500/bus && break; sleep 1; done
B sh -c 'ls -l /dev/kvm; id tester; command -v qemu-system-x86_64 || echo "no QEMU yet"'

say "npx cseries, twice"
U node /opt/e2e/tty-driver.js node /opt/e2e/e2e-lima.js /opt/cseries-cli /e2e/guest.qcow2 /e2e/privatae-bastion /e2e/images.tar \
    "localhost/${host_img%%:*}:${host_img##*:}" "localhost/${pg_img%%:*}:${pg_img##*:}"

say "what the machine reaches"
B curl -s -o /dev/null -w 'GET 127.0.0.1:8800/api/system/health -> %{http_code}\n' http://127.0.0.1:8800/api/system/health
B curl -sk -o /dev/null -w 'GET https://127.0.0.1:8810/ -> %{http_code}\n' https://127.0.0.1:8810/
B sh -c 'ss -ltnH | awk "{print \$4}" | sort'
limactl=$(U sh -c 'ls ~/.local/share/cseries/tools/lima-*/bin/limactl')
U env LIMA_HOME=/home/tester/.local/share/cseries/lima "$limactl" --tty=false shell --workdir / cseries sh -c \
    'nohup python3 -c "import socket,time;s=socket.socket();s.bind((\"127.0.0.1\",9999));s.listen();time.sleep(20)" >/dev/null 2>&1 &'
sleep 5
if B sh -c 'exec 3<>/dev/tcp/127.0.0.1/9999' 2>/dev/null; then echo "FAIL: guest port 9999 reached the machine"; exit 1; fi
echo "a guest port other than 8800, 8810, 8811 is not forwarded"
# A program on the machine reaching the Host through the forward, with no
# owner session: the Host must see a peer that is not its own loopback and
# send it to sign in (server/middleware/localhost_only.py). Were the forward
# to arrive as 127.0.0.1, every program on the machine would have the owner's API.
status=$(B curl -s -w ' %{http_code}' http://127.0.0.1:8800/api/system/status)
case "$status" in
    *'sign in first'*' 403') echo "an API call from the machine with no session is sent to sign in" ;;
    *) echo "FAIL: an API call from the machine with no session got: $status"; exit 1 ;;
esac

say "the local runtime on the guest's own podman and systemd"
# The containers' slice and an app's kept data out and back, as root in the
# guest, with the Host's own client in the Host's container.
Ui env LIMA_HOME=/home/tester/.local/share/cseries/lima "$limactl" --tty=false shell --workdir / cseries \
    sudo bash -s -- "localhost/${host_img%%:*}:${host_img##*:}" < "$repo/vm/runtime/test/real-podman-checks.sh"

say "the bastion"
B systemctl is-active privatae-bastion
B systemctl show privatae-bastion -p User -p ExecStart --value | head -2
B env HOME=/root /usr/local/bin/privatae-bastion status 2>&1 | head -20
B sudo -u tester sh -c '/usr/local/bin/privatae-bastion status 2>&1 | head -3; ls /root 2>&1 | head -1'

say "the login unit starts the VM in a fresh user manager"
U systemctl --user is-enabled cseries.service
U env LIMA_HOME=/home/tester/.local/share/cseries/lima "$limactl" --tty=false stop cseries 2>&1 | tail -1
B systemctl restart user@1500.service
for _ in $(seq 1 30); do B test -S /run/user/1500/bus && break; sleep 1; done
U systemctl --user start cseries.service
# The VM boots, then the Host starts as on a first run: give it ten minutes,
# and say how long it took.
ok="" t0=$(date +%s)
for _ in $(seq 1 200); do
    if B curl -sf -o /dev/null http://127.0.0.1:8800/api/system/health; then ok=1; break; fi
    sleep 3
done
if [ -z "$ok" ]; then
    echo "FAIL: the Host did not come back through the login unit within $(( $(date +%s) - t0 )) s"
    U systemctl --user status cseries.service --no-pager | tail -20
    U env LIMA_HOME=/home/tester/.local/share/cseries/lima "$limactl" --tty=false shell --workdir / cseries sudo /usr/local/bin/cseries-guest status || true
    U env LIMA_HOME=/home/tester/.local/share/cseries/lima "$limactl" --tty=false shell --workdir / cseries sudo /usr/local/bin/cseries-guest logs host --lines 30 || true
    exit 1
fi
echo "the Host answers again after the login unit started the VM ($(( $(date +%s) - t0 )) s)"

say "uninstall --delete-data"
U systemctl --user stop cseries.service
U node -e 'require("/opt/cseries-cli/src/cli").main(["uninstall", "--delete-data", "--yes"]).then((c) => process.exit(c))'
U sh -c 'test ! -e ~/.local/share/cseries/lima && test ! -e ~/.config/systemd/user/cseries.service && echo "the VM, its folder and the login unit are gone"'

say "PASS"
