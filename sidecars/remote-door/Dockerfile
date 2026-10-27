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
FROM caddy:2.11.7-alpine@sha256:eb4d35b7452d1ab6171fbdbe8d9275b2301dad6dad92115d91e19fb9b24d510f

# Links the published package to its source, as the Host and postgres images do
# (docs/RELEASING.md).
LABEL org.opencontainers.image.source="https://github.com/Privatae/cseries"

# Exact versions, from the Alpine branch the base image above is (3.23,
# read from its /etc/alpine-release on 2026-10-26), so the same build inputs
# make the same image. lego is held to the 4.29 series because door-cert
# writes the account into lego 4.29's own layout (door-cert LEGO_SERIES
# checks it at run time too). When Alpine replaces a revision, this build
# fails until the pins are moved to the new one; move lego off 4.29 only with
# door-cert's account layout checked against the new series.
#
# The caddy base image gives /usr/bin/caddy, and Alpine's package /usr/bin/lego,
# the file capability cap_net_bind_service=+ep. The door and its certificate
# job run with an empty bounding set (compose.remote.yaml's cap_drop and
# door-filter, the guests' DropCapability=ALL), and the kernel refuses to run a
# binary with an effective file capability it cannot grant: "exec: Operation
# not permitted", so Caddy never starts and lego never makes a certificate.
# Neither needs it (Caddy listens on 8443; lego answers DNS-01 through
# cseries-acme-dns-hook), so both lose it here.
RUN apk add --no-cache python3=3.12.15-r0 lego=4.29.0-r6 nftables=1.1.5-r2 \
    && test -x /usr/bin/lego && test -x /usr/bin/python3 && test -x /usr/sbin/nft \
    && /usr/bin/lego --version | grep -q ' 4\.29\.' \
    && /usr/bin/python3 -I -c 'import os; fc = "security.capability"; bins = ("/usr/bin/caddy", "/usr/bin/lego"); [os.removexattr(b, fc) for b in bins if fc in os.listxattr(b)]; left = [b for b in bins if fc in os.listxattr(b)]; raise SystemExit(f"file capabilities left on {left}" if left else 0)'

COPY vm/cloud/overlay/usr/local/lib/cseries-cloud/cseries_cloud.py /usr/local/lib/cseries-cloud/cseries_cloud.py
COPY vm/cloud/overlay/usr/local/sbin/cseries-cert /usr/local/sbin/cseries-cert
COPY vm/cloud/overlay/usr/local/sbin/cseries-acme-dns-hook /usr/local/sbin/cseries-acme-dns-hook
COPY sidecars/remote-door/Caddyfile /etc/caddy/Caddyfile
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
