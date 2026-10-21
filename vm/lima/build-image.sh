#!/usr/bin/env bash
# Builds the guest image for Lima (macOS and Linux): a bootable Debian disk
# with the same packages, units and cseries-guest as the WSL guest, plus what
# a virtual machine of its own needs to boot and to be driven by Lima (a
# kernel, systemd-boot, cloud-init, sshd, sudo).
#
#   vm/lima/build-image.sh --arch x64|arm64 --version X.Y.Z --out <dir>
#
# Writes <dir>/cseries-guest-X.Y.Z-<arch>.qcow2 and its .sha256.
#
# The WSL guest's package list (vm/wsl/packages), its overlay of units and
# cseries-guest (vm/wsl/rootfs), its Debian snapshot and its builder image
# (vm/wsl/build-rootfs.sh) are read from there, and the local runtime
# (vm/runtime) is installed as the WSL guest installs it, so the two guests
# cannot drift apart. Like that build, this one needs Docker on Linux and nothing else, and
# builds the machine's own architecture unless qemu-user is registered.
set -euo pipefail

usage() {
    echo "usage: $0 --arch x64|arm64 --version X.Y.Z --out <dir>" >&2
    exit 2
}

arch="" version="" out=""
while [ $# -gt 0 ]; do
    case "$1" in
        --arch) arch=${2:-}; shift 2 ;;
        --version) version=${2:-}; shift 2 ;;
        --out) out=${2:-}; shift 2 ;;
        *) usage ;;
    esac
done
case "$arch" in
    x64) debarch=amd64; machine=x86_64 ;;
    arm64) debarch=arm64; machine=aarch64 ;;
    *) usage ;;
esac
[[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+([.+-][0-9A-Za-z.+-]+)?$ ]] || {
    echo "--version must look like 1.2.3 (got '$version')" >&2
    exit 2
}
[ -n "$out" ] || usage

here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
wsl="$repo/vm/wsl"
seccomp="$repo/seccomp-personal.json"
for f in "$seccomp" "$repo/seccomp-browser.json" "$repo/vm/runtime/sbin/cseries-runtime" \
    "$wsl/packages" "$wsl/build-rootfs.sh" "$wsl/rootfs/usr/local/bin/cseries-guest"; do
    [ -f "$f" ] || { echo "missing $f" >&2; exit 1; }
done
command -v docker >/dev/null || { echo "docker is needed to build the guest image" >&2; exit 1; }

# The WSL build's pins, read from it.
pin() {
    local v
    v=$(sed -n "s/^$1=//p" "$wsl/build-rootfs.sh")
    [ -n "$v" ] || { echo "cannot read $1 from $wsl/build-rootfs.sh" >&2; exit 1; }
    printf '%s' "$v"
}
SUITE=$(pin SUITE)
SNAPSHOT=$(pin SNAPSHOT)
BUILDER_IMAGE=$(pin BUILDER_IMAGE)

if [ "$(uname -m)" != "$machine" ] && [ ! -e "/proc/sys/fs/binfmt_misc/qemu-$machine" ]; then
    echo "Building the $arch guest on $(uname -m) needs the qemu-$machine binfmt handler," >&2
    echo "which this machine does not have. Build it on a $arch machine instead." >&2
    exit 1
fi

mkdir -p "$out"
out=$(cd "$out" && pwd)
name="cseries-guest-$version-$arch.qcow2"

docker run --rm --privileged \
    -e SUITE="$SUITE" -e SNAPSHOT="$SNAPSHOT" -e DEBARCH="$debarch" \
    -e GUEST_VERSION="$version" -e OUT_NAME="$name" \
    -e OUT_UID="$(id -u)" -e OUT_GID="$(id -g)" \
    -v "$here:/src:ro" \
    -v "$wsl:/src-wsl:ro" \
    -v "$seccomp:/src-seccomp/seccomp-personal.json:ro" \
    -v "$repo/seccomp-browser.json:/src-seccomp/seccomp-browser.json:ro" \
    -v "$repo/vm/runtime:/src-runtime:ro" \
    -v "$out:/out" \
    "$BUILDER_IMAGE" \
    bash /src/mkimage.sh

echo "$out/$name"
cat "$out/$name.sha256"
