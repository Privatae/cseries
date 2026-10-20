#!/usr/bin/env bash
# mmdebstrap's customize hook (mkimage.sh): adds the cseries files to the
# fresh Debian tree at $1. Runs inside the builder container.
set -euo pipefail
root=$1

# The WSL guest's overlay (the Host's units, Podman's settings and
# cseries-guest), without the two files only WSL reads.
cp -R /src-wsl/rootfs/. "$root/"
(cd /src-wsl/rootfs && find . -type d) | while read -r d; do chmod 0755 "$root/$d"; done
(cd /src-wsl/rootfs && find . -type f) | while read -r f; do chmod 0644 "$root/$f"; done
rm -f "$root/etc/wsl.conf" "$root/etc/wsl-distribution.conf"
chmod 0755 "$root/usr/local/bin/cseries-guest"

# This guest's own files: cloud-init's settings, the network fallback, fstab.
cp -R /src/rootfs/. "$root/"
(cd /src/rootfs && find . -type d) | while read -r d; do chmod 0755 "$root/$d"; done
(cd /src/rootfs && find . -type f) | while read -r f; do chmod 0644 "$root/$f"; done

# The Host's seccomp profile, the same file compose.yaml names.
install -D -m 0644 /src-seccomp/seccomp-personal.json "$root/usr/share/cseries/seccomp-personal.json"

install -d -m 0755 "$root/etc/cseries"
printf '%s\n' "$GUEST_VERSION" > "$root/etc/cseries/guest-version"

# No user of uid 1000 here, unlike the WSL guest: the Host's containers run as
# 1000, and Lima's own user (with sudo) is given another uid by the cseries
# command, so a process of uid 1000 that left its container is nobody.

chroot "$root" systemctl enable systemd-networkd.service systemd-resolved.service \
    systemd-timesyncd.service ssh.service
# The first-boot prompt would wait on a console nobody watches.
chroot "$root" systemctl mask systemd-firstboot.service

# Each VM makes its own SSH host keys on first boot (cloud-init).
rm -f "$root"/etc/ssh/ssh_host_*

# apt in the guest reads the live archive, not the build's snapshot.
rm -f "$root/etc/apt/sources.list"
cat > "$root/etc/apt/sources.list.d/debian.sources" <<'EOF'
Types: deb
URIs: http://deb.debian.org/debian
Suites: trixie trixie-updates
Components: main
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg

Types: deb
URIs: http://deb.debian.org/debian-security
Suites: trixie-security
Components: main
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg
EOF
