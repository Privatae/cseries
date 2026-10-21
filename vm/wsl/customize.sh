#!/usr/bin/env bash
# mmdebstrap's customize hook (mkrootfs.sh): adds the cseries files to the
# fresh Debian tree at $1. Runs inside the builder container.
set -euo pipefail
root=$1

# The overlay, copied as root so root owns it, with fixed modes whatever the
# checkout's umask.
cp -R /src/rootfs/. "$root/"
(cd /src/rootfs && find . -type d) | while read -r d; do chmod 0755 "$root/$d"; done
(cd /src/rootfs && find . -type f) | while read -r f; do chmod 0644 "$root/$f"; done
chmod 0755 "$root/usr/local/bin/cseries-guest"

# The Host's seccomp profile, the same file compose.yaml names, and the
# browser's, which the local runtime gives the browser container.
install -D -m 0644 /src-seccomp/seccomp-personal.json "$root/usr/share/cseries/seccomp-personal.json"
install -D -m 0644 /src-seccomp/seccomp-browser.json "$root/usr/share/cseries/seccomp-browser.json"

# The local runtime (vm/runtime): its code, units, network and plain proxy's
# config, the same files a cloud box installs (vm/cloud/build-overlay.sh).
install -d -m 0755 "$root/usr/local/lib/cseries-runtime/cseries_runtime"
install -m 0644 /src-runtime/lib/cseries_runtime/*.py "$root/usr/local/lib/cseries-runtime/cseries_runtime/"
install -D -m 0755 /src-runtime/sbin/cseries-runtime "$root/usr/local/sbin/cseries-runtime"
install -D -m 0755 /src-runtime/sbin/cseries-build-user "$root/usr/local/sbin/cseries-build-user"
install -m 0644 /src-runtime/systemd/* "$root/etc/systemd/system/"
install -m 0644 /src-runtime/containers/cseries-apps.network "$root/etc/containers/systemd/cseries-apps.network"
install -D -m 0644 /src-runtime/squid.conf "$root/etc/cseries-runtime/squid.conf"
chroot "$root" groupadd --system cseries-runtime
# The user the coding engines are built as (cseries_runtime/build.py).
chroot "$root" /usr/local/sbin/cseries-build-user
chroot "$root" systemctl enable cseries-runtime.socket cseries-squid.service cseries-runtime-firewall.service
# Debian's own squid would listen on every address with its default rules.
chroot "$root" systemctl mask squid.service

install -d -m 0755 "$root/etc/cseries"
printf '%s\n' "$GUEST_VERSION" > "$root/etc/cseries/guest-version"

# The default user WSL logs in as. No password: WSL starts a shell as this
# user without one, and root is `wsl -u root`, so no sudo either.
chroot "$root" useradd --uid 1000 --user-group --create-home --shell /bin/bash cseries

# Units WSL's guide for a distribution with systemd says to mask, and the
# first-boot prompt, which would wait on a console WSL does not have.
chroot "$root" systemctl mask \
    systemd-resolved.service systemd-networkd.service NetworkManager.service \
    systemd-tmpfiles-setup.service systemd-tmpfiles-clean.service \
    systemd-tmpfiles-clean.timer systemd-tmpfiles-setup-dev-early.service \
    systemd-tmpfiles-setup-dev.service tmp.mount systemd-firstboot.service

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
