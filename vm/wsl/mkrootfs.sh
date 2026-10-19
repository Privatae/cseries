#!/usr/bin/env bash
# Runs inside the builder container that build-rootfs.sh starts; not meant to
# be run by hand. Reads SUITE, SNAPSHOT, DEBARCH, GUEST_VERSION, OUT_NAME,
# OUT_UID and OUT_GID from the environment, /src (this directory) and
# /src-seccomp, and writes /out/$OUT_NAME and its .sha256.
set -euo pipefail
export LC_ALL=C.UTF-8 TZ=UTC

ts=$SNAPSHOT
SOURCE_DATE_EPOCH=$(date -u -d "${ts:0:4}-${ts:4:2}-${ts:6:2}T${ts:9:2}:${ts:11:2}:${ts:13:2}Z" +%s)
export SOURCE_DATE_EPOCH

mirror=http://snapshot.debian.org/archive
# apt checks every file against the archive's signed Release, so plain HTTP
# carries no trust; the snapshot's Release files are past their Valid-Until.
noexpire='Acquire::Check-Valid-Until "false";'

# The build tool comes from the same snapshot as the guest.
rm -f /etc/apt/sources.list
cat > /etc/apt/sources.list.d/debian.sources <<EOF
Types: deb
URIs: $mirror/debian/$SNAPSHOT
Suites: $SUITE $SUITE-updates
Components: main
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg

Types: deb
URIs: $mirror/debian-security/$SNAPSHOT
Suites: $SUITE-security
Components: main
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg
EOF
echo "builder: installing mmdebstrap from snapshot $SNAPSHOT"
apt-get -o Acquire::Check-Valid-Until=false update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends mmdebstrap >/dev/null

pkgs=$(grep -v '^#' /src/packages | grep -v '^$' | paste -sd, -)
root=/work/rootfs
mkdir -p /work

echo "builder: debian $SUITE ($DEBARCH) with: $pkgs"
mmdebstrap --mode=root --variant=apt --arch="$DEBARCH" \
    --include="$pkgs" \
    --aptopt="$noexpire" \
    --aptopt='APT::Install-Recommends "false"' \
    --customize-hook='bash /src/customize.sh "$1"' \
    "$SUITE" "$root" \
    "deb $mirror/debian/$SNAPSHOT $SUITE main" \
    "deb $mirror/debian/$SNAPSHOT $SUITE-updates main" \
    "deb $mirror/debian-security/$SNAPSHOT $SUITE-security main"

# What changes from one build to the next and carries nothing: logs, caches,
# the build's own resolver and machine id. WSL writes /etc/resolv.conf itself.
cd "$root"
rm -rf var/lib/apt/lists/* var/cache/apt/* var/cache/debconf/*-old \
    var/cache/ldconfig/aux-cache var/lib/dbus/machine-id \
    etc/resolv.conf tmp/* run/* dev/* proc/* sys/*
find var/log -type f -delete
mkdir -p var/log/journal
: > etc/machine-id
printf 'cseries\n' > etc/hostname

# Every timestamp is the snapshot's.
find . -newermt "@$SOURCE_DATE_EPOCH" -print0 | xargs -0r touch -h -d "@$SOURCE_DATE_EPOCH"

# The root of the archive is the root of the filesystem, entries in name
# order with numeric owners, as WSL's guide for a distribution's tar asks.
# shellcheck disable=SC2046
tar --sort=name --numeric-owner --format=pax \
    --pax-option=exthdr.name=%d/PaxHeaders/%f,delete=atime,delete=ctime \
    --mtime="@$SOURCE_DATE_EPOCH" --clamp-mtime \
    -cf - $(LC_ALL=C ls -A) | gzip -n -9 > "/out/$OUT_NAME"
cd /out
sha256sum "$OUT_NAME" > "$OUT_NAME.sha256"
chown "$OUT_UID:$OUT_GID" "$OUT_NAME" "$OUT_NAME.sha256"
echo "builder: $(du -h "$OUT_NAME" | cut -f1) $OUT_NAME"
