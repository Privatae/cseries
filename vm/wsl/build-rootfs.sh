#!/usr/bin/env bash
# Builds the WSL2 guest image: a minimal Debian root filesystem with systemd,
# Podman, and the units that run the Host and its database with the hardening
# compose.yaml gives them.
#
#   vm/wsl/build-rootfs.sh --arch x64|arm64 --version X.Y.Z --out <dir>
#
# Writes <dir>/cseries-wsl-rootfs-X.Y.Z-<arch>.tar.gz and its .sha256.
#
# Needs Docker on Linux, and nothing else installed here: the build runs
# mmdebstrap inside a pinned Debian image. Building for the other architecture
# needs that architecture's qemu-user binfmt handler on this machine; the
# release workflow builds each one natively instead.
#
# The same inputs give the same bytes: the packages come from one pinned
# snapshot of the Debian archive, the build tool from a pinned image, every
# timestamp in the archive is the snapshot's time, and gzip stores no name or
# time.
set -euo pipefail

# Debian trixie, because the units are Quadlet files (Podman 4.4 or later;
# bookworm has 4.3).
SUITE=trixie
# snapshot.debian.org timestamp the packages come from. Moving it is how the
# guest takes package updates.
SNAPSHOT=20261015T000000Z
BUILDER_IMAGE=debian:trixie-slim@sha256:a29215f6a35e51e22adffa17f89e9d2ef06214e64a2bad10d765c46aea49f11f

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
seccomp="$repo/seccomp-personal.json"
[ -f "$seccomp" ] || { echo "missing $seccomp" >&2; exit 1; }
command -v docker >/dev/null || { echo "docker is needed to build the guest image" >&2; exit 1; }

if [ "$(uname -m)" != "$machine" ] && [ ! -e "/proc/sys/fs/binfmt_misc/qemu-$machine" ]; then
    echo "Building the $arch guest on $(uname -m) needs the qemu-$machine binfmt handler," >&2
    echo "which this machine does not have. Build it on a $arch machine instead." >&2
    exit 1
fi

mkdir -p "$out"
out=$(cd "$out" && pwd)
name="cseries-wsl-rootfs-$version-$arch.tar.gz"

# The builder runs on this machine's own architecture; mmdebstrap --arch makes
# the guest's.
docker run --rm --privileged \
    -e SUITE="$SUITE" -e SNAPSHOT="$SNAPSHOT" -e DEBARCH="$debarch" \
    -e GUEST_VERSION="$version" -e OUT_NAME="$name" \
    -e OUT_UID="$(id -u)" -e OUT_GID="$(id -g)" \
    -v "$here:/src:ro" \
    -v "$seccomp:/src-seccomp/seccomp-personal.json:ro" \
    -v "$out:/out" \
    "$BUILDER_IMAGE" \
    bash /src/mkrootfs.sh

echo "$out/$name"
cat "$out/$name.sha256"
