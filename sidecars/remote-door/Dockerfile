# The door on a member's own machine, for remote access (compose.remote.yaml).
#
# Caddy, upstream 2.11.7, pinned by digest (the multi-arch index of
# caddy:2.11.7-alpine, read from Docker Hub on 2026-10-26): its standard
# proxy_protocol listener wrapper reads the browser's address the Host's
# tunnel writes ahead of each connection. Python and lego are for the
# certificate job (door-cert), which is the cloud box's own cseries-cert and
# cseries-acme-dns-hook, copied here unchanged with the module they import.
# nft is for door-filter, the door's own firewall in the Docker Compose
# install (compose.remote.yaml), where nothing else keeps the door to the
# Host's web port; the WSL and Lima guests filter on their bridge instead and
# run door-run directly.
#
# Build from the repository root:
#   docker build -f sidecars/remote-door/Dockerfile -t cseries-remote-door:dev .

# The same Caddy release, built from source with a newer Go. The binary in
# caddy:2.11.7-alpine was built with Go 1.26.8, whose standard library the
# release scan fails on (CVE-2026-56857, CRITICAL, fixed in Go 1.26.9 and
# 1.27.2). Caddy's official builder image, pinned by its multi-arch index
# (read from Docker Hub on 2026-10-28; Go 1.27.3, xcaddy 0.4.7), builds
# v2.11.7 with Caddy's standard modules, the set the base image's binary has.
# Caddy's go.mod fixes every module's version; xcaddy takes them from the Go
# module proxy, and the go command checks each against the Go checksum
# database. GOTOOLCHAIN=local (that image sets it too) keeps a toolchain line
# from fetching another Go: a newer requirement fails the build instead.
# CGO_ENABLED=0 makes the binary static, as the one it replaces is.
# XCADDY_SETCAP=0, because the image sets 1: the capability would be stripped
# again below, and stripping it copies the whole binary into another layer.
FROM caddy:2.11.7-builder@sha256:bf6a7d38add2508b7d2e3e3f40cf2af399214f143c0fa3daa51f254e01f8d293 AS caddy-build
RUN GOTOOLCHAIN=local CGO_ENABLED=0 XCADDY_SETCAP=0 xcaddy build v2.11.7 --output /out/caddy \
    && /out/caddy version | grep -q '^v2\.11\.7 ' \
    && go version -m /out/caddy | grep -q 'CGO_ENABLED=0' \
    && /out/caddy list-modules | grep -qx 'caddy.listeners.proxy_protocol'

FROM caddy:2.11.7-alpine@sha256:eb4d35b7452d1ab6171fbdbe8d9275b2301dad6dad92115d91e19fb9b24d510f
COPY --from=caddy-build /out/caddy /usr/bin/caddy

# Links the published package to its source, as the Host and postgres images do
# (docs/RELEASING.md).
LABEL org.opencontainers.image.source="https://github.com/Privatae/cseries"

# Version series, from the Alpine branch the base image above is (3.23,
# read from its /etc/alpine-release on 2026-10-26). The base is pinned by
# digest, but apk resolves against the branch's live index, which keeps only
# the newest revision of each package: an exact revision pin (3.12.15-r0 was
# one) breaks the release build the day Alpine replaces it. A series pin
# (~X.Y, matched by version component: python3~3.12 takes 3.12.16, never
# 3.120 or 3.13) takes that branch's current revision of the series and still
# refuses a new series. The cost: two builds of this file can carry
# different revisions within a series, as they always could for the
# libraries these three pull in, which are not pinned. What a release ships
# is fixed by the door image's digest in release.json, and the release job
# scans that archive, the one it pushes (.github/workflows/release.yml).
# lego is held to the 4.29 series because door-cert writes the
# account into lego 4.29's own layout (door-cert LEGO_SERIES checks it at run
# time too, and the build checks it below); move lego off 4.29 only with
# door-cert's account layout checked against the new series.
#
# The caddy images give /usr/bin/caddy, and Alpine's package /usr/bin/lego,
# the file capability cap_net_bind_service=+ep (the build above leaves it off
# its binary, and this still checks). The door and its certificate
# job run with an empty bounding set (compose.remote.yaml's cap_drop and
# door-filter, the guests' DropCapability=ALL), and the kernel refuses to run a
# binary with an effective file capability it cannot grant: "exec: Operation
# not permitted", so Caddy never starts and lego never makes a certificate.
# Neither needs it (Caddy listens on 8443; lego answers DNS-01 through
# cseries-acme-dns-hook), so both lose it here.
# `apk upgrade` first takes the branch's current revision of every package the
# base image already has, so a library Alpine has fixed since the base was
# built (brotli-libs 1.2.0-r0 to 1.2.1-r0 for CVE-2026-111497, 2026-10-28)
# does not fail the release scan. It is the same live index the add below
# resolves against.
RUN apk upgrade --no-cache \
    && apk add --no-cache python3~3.12 lego~4.29 nftables~1.1 \
    && test -x /usr/bin/lego && test -x /usr/bin/python3 && test -x /usr/sbin/nft \
    && /usr/bin/lego --version | grep -q ' 4\.29\.' \
    && /usr/bin/python3 -I -c 'import os; fc = "security.capability"; bins = ("/usr/bin/caddy", "/usr/bin/lego"); [os.removexattr(b, fc) for b in bins if fc in os.listxattr(b)]; left = [b for b in bins if fc in os.listxattr(b)]; raise SystemExit(f"file capabilities left on {left}" if left else 0)'

COPY vm/cloud/overlay/usr/local/lib/cseries-cloud/cseries_cloud.py /usr/local/lib/cseries-cloud/cseries_cloud.py
COPY vm/cloud/overlay/usr/local/sbin/cseries-cert /usr/local/sbin/cseries-cert
COPY vm/cloud/overlay/usr/local/sbin/cseries-acme-dns-hook /usr/local/sbin/cseries-acme-dns-hook
COPY sidecars/remote-door/Caddyfile /etc/caddy/Caddyfile
# The door's rules, the one file the cloud box's front door imports too.
COPY vm/cloud/overlay/etc/caddy/host-door.caddy /etc/caddy/host-door.caddy
COPY sidecars/remote-door/door-run sidecars/remote-door/door-cert sidecars/remote-door/door-filter /usr/local/sbin/

# Every place the door and its certificate job write, owned by the user they
# run as, so the named volumes mounted there start with that owner. The
# certificate's key and the ACME account key are made in these and stay in
# them.
RUN mkdir -p /etc/caddy/tls /var/lib/cseries-cloud /etc/cseries-cloud /data/door-key /run/caddy \
        /var/lib/cseries-cert-status \
    && chown 1000:1000 /etc/caddy/tls /var/lib/cseries-cloud /data/door-key /run/caddy \
        /var/lib/cseries-cert-status \
    && chmod 0700 /etc/caddy/tls /var/lib/cseries-cloud \
    && chmod 0750 /data/door-key

USER 1000:1000
ENV CSERIES_DOOR_TLS=/etc/caddy/tls XDG_DATA_HOME=/run/caddy XDG_CONFIG_HOME=/run/caddy
ENTRYPOINT ["/usr/local/sbin/door-run"]
