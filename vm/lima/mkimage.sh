#!/usr/bin/env bash
# Runs inside the builder container that build-image.sh starts; not meant to
# be run by hand. Reads SUITE, SNAPSHOT, DEBARCH, GUEST_VERSION, OUT_NAME,
# OUT_UID and OUT_GID from the environment, /src (vm/lima), /src-wsl (vm/wsl)
# and /src-seccomp, and writes /out/$OUT_NAME and its .sha256.
#
# The disk is put together from files, with no loop device and no mount:
#
#   GPT, label and partition GUIDs fixed
#   1  EFI system partition, FAT32, 128 MiB: systemd-boot as the removable
#      boot path (EFI/BOOT/BOOT<arch>.EFI, so no firmware variable is needed),
#      one loader entry, and the kernel and initrd it boots
#   2  the root filesystem, ext4 labelled cseries-root, made with mke2fs -d
#      from the tree mmdebstrap built; cloud-init grows it to the disk size
#      Lima gives the VM on first boot
#
# then converted to a compressed qcow2, which Lima reads on both QEMU and vz.
set -euo pipefail
export LC_ALL=C.UTF-8 TZ=UTC

ts=$SNAPSHOT
SOURCE_DATE_EPOCH=$(date -u -d "${ts:0:4}-${ts:4:2}-${ts:6:2}T${ts:9:2}:${ts:11:2}:${ts:13:2}Z" +%s)
export SOURCE_DATE_EPOCH
# mke2fs stamps its own times from this.
export E2FSPROGS_FAKE_TIME=$SOURCE_DATE_EPOCH

case "$DEBARCH" in
    amd64)
        efiarch=x64
        # x86-64 root partition type (Discoverable Partitions Specification)
        roottype=4F68BCE3-E8CD-4DB1-96E7-FBCAF984B709
        consoles="console=tty0 console=hvc0 console=ttyS0,115200n8"
        ;;
    arm64)
        efiarch=aa64
        roottype=B921B045-1DF0-41C3-AF44-4C6F280D3FAE
        consoles="console=tty0 console=hvc0 console=ttyAMA0"
        ;;
    *) echo "unknown DEBARCH $DEBARCH" >&2; exit 1 ;;
esac
EFIARCH_UPPER=$(printf '%s' "$efiarch" | tr '[:lower:]' '[:upper:]')

# Fixed identities: one guest per machine, and the same input gives the same
# disk.
DISK_GUID=5c1d0d1e-63a1-4c9e-9f6e-0c5e21e50001
ESP_GUID=5c1d0d1e-63a1-4c9e-9f6e-0c5e21e50002
ROOT_GUID=5c1d0d1e-63a1-4c9e-9f6e-0c5e21e50003
ROOT_FS_UUID=5c1d0d1e-63a1-4c9e-9f6e-0c5e21e50004
ESP_VOLID=0C5E21E5

mirror=http://snapshot.debian.org/archive
# apt checks every file against the archive's signed Release, so plain HTTP
# carries no trust; the snapshot's Release files are past their Valid-Until.
noexpire='Acquire::Check-Valid-Until "false";'

# The build tools come from the same snapshot as the guest.
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
echo "builder: installing the build tools from snapshot $SNAPSHOT"
apt-get -o Acquire::Check-Valid-Until=false update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
    mmdebstrap dosfstools mtools e2fsprogs fdisk qemu-utils >/dev/null

list() { grep -v '^#' "$1" | grep -v '^$'; }
pkgs=$( { list /src-wsl/packages; list /src/packages; echo "linux-image-cloud-$DEBARCH"; } | paste -sd, -)
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

# What changes from one build to the next and carries nothing, as in the WSL
# build. /etc/resolv.conf stays when it is systemd-resolved's link.
cd "$root"
rm -rf var/lib/apt/lists/* var/cache/apt/* var/cache/debconf/*-old \
    var/cache/ldconfig/aux-cache var/lib/dbus/machine-id \
    tmp/* run/* dev/* proc/* sys/*
[ -L etc/resolv.conf ] || rm -f etc/resolv.conf
find var/log -type f -delete
mkdir -p var/log/journal
: > etc/machine-id
printf 'cseries\n' > etc/hostname

# The kernel and initrd systemd-boot loads, from the ESP.
kernel=$(find boot -maxdepth 1 -name 'vmlinuz-*' | sort | tail -n 1)
[ -n "$kernel" ] || { echo "no kernel in /boot" >&2; exit 1; }
kver=${kernel#boot/vmlinuz-}
[ -f "boot/initrd.img-$kver" ] || { echo "no initrd for $kver" >&2; exit 1; }
efi="usr/lib/systemd/boot/efi/systemd-boot$efiarch.efi"
[ -f "$efi" ] || { echo "no $efi" >&2; exit 1; }

esp=/work/esp
mkdir -p "$esp/EFI/BOOT" "$esp/loader/entries" "$esp/cseries"
cp "$efi" "$esp/EFI/BOOT/BOOT$EFIARCH_UPPER.EFI"
cp "boot/vmlinuz-$kver" "$esp/cseries/vmlinuz"
cp "boot/initrd.img-$kver" "$esp/cseries/initrd.img"
printf 'timeout 0\ndefault cseries.conf\neditor no\n' > "$esp/loader/loader.conf"
cat > "$esp/loader/entries/cseries.conf" <<EOF
title C-Series guest ($kver)
linux /cseries/vmlinuz
initrd /cseries/initrd.img
options root=LABEL=cseries-root rw net.ifnames=0 $consoles
EOF

# Every timestamp is the snapshot's.
find "$root" "$esp" -newermt "@$SOURCE_DATE_EPOCH" -print0 | xargs -0r touch -h -d "@$SOURCE_DATE_EPOCH"
cd /work

mib() { echo $(( ($1 + 1048575) / 1048576 )); }
esp_mib=128
used=$(du -sx --block-size=1 "$root" | cut -f1)
# A third more than the files, and 256 MiB, so the first boot has room before
# cloud-init grows the filesystem.
root_mib=$(( $(mib "$used") * 4 / 3 + 256 ))

rm -f esp.img root.img disk.raw
mkfs.fat -C -F 32 -n CSERIES-ESP -i "$ESP_VOLID" esp.img $(( esp_mib * 1024 )) >/dev/null
mcopy -s -m -i esp.img "$esp"/* ::/
mkfs.ext4 -q -L cseries-root -U "$ROOT_FS_UUID" \
    -E "root_owner=0:0,hash_seed=$ROOT_FS_UUID" \
    -d "$root" root.img "${root_mib}M"

# 1 MiB in front for the GPT, 1 MiB behind for its backup.
total_mib=$(( 1 + esp_mib + root_mib + 1 ))
truncate -s "${total_mib}M" disk.raw
sfdisk --quiet disk.raw <<EOF
label: gpt
label-id: $DISK_GUID
unit: sectors
first-lba: 2048
start=2048, size=$(( esp_mib * 2048 )), type=C12A7328-F81F-11D2-BA4B-00A0C93EC93B, uuid=$ESP_GUID, name="EFI system"
start=$(( (1 + esp_mib) * 2048 )), size=$(( root_mib * 2048 )), type=$roottype, uuid=$ROOT_GUID, name="cseries-root"
EOF
dd if=esp.img of=disk.raw bs=1M seek=1 conv=notrunc status=none
dd if=root.img of=disk.raw bs=1M seek=$(( 1 + esp_mib )) conv=notrunc status=none

qemu-img convert -f raw -O qcow2 -c disk.raw "/out/$OUT_NAME"
cd /out
sha256sum "$OUT_NAME" > "$OUT_NAME.sha256"
chown "$OUT_UID:$OUT_GID" "$OUT_NAME" "$OUT_NAME.sha256"
echo "builder: $(du -h "$OUT_NAME" | cut -f1) $OUT_NAME (kernel $kver, root ${root_mib} MiB)"
