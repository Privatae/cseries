# The door on a member's own machine, for remote access (compose.remote.yaml).
#
# Caddy, upstream 2.11.2, pinned by digest (the multi-arch index of
# caddy:2.11.2-alpine, read from Docker Hub on 2026-10-25): its standard
# proxy_protocol listener wrapper reads the browser's address the Host's
# tunnel writes ahead of each connection. Python and lego are for the
# certificate job (door-cert), which is the cloud box's own cseries-cert and
# cseries-acme-dns-hook, copied here unchanged with the module they import.
#
# Build from the repository root:
#   docker build -f sidecars/remote-door/Dockerfile -t cseries-remote-door:dev .
FROM caddy:2.11.2-alpine@sha256:834468128c7696cec0ceea6172f7d692daf645ae51983ca76e39da54a97c570d

RUN apk add --no-cache python3 lego && test -x /usr/bin/lego && test -x /usr/bin/python3

COPY vm/cloud/overlay/usr/local/lib/cseries-cloud/cseries_cloud.py /usr/local/lib/cseries-cloud/cseries_cloud.py
COPY vm/cloud/overlay/usr/local/sbin/cseries-cert /usr/local/sbin/cseries-cert
COPY vm/cloud/overlay/usr/local/sbin/cseries-acme-dns-hook /usr/local/sbin/cseries-acme-dns-hook
COPY sidecars/remote-door/Caddyfile /etc/caddy/Caddyfile
COPY sidecars/remote-door/door-run sidecars/remote-door/door-cert /usr/local/sbin/

# Every place the door and its certificate job write, owned by the user they
# run as, so the named volumes mounted there start with that owner. The
# certificate's key and the ACME account key are made in these and stay in
# them.
RUN mkdir -p /etc/caddy/tls /var/lib/cseries-cloud /etc/cseries-cloud /data/door-key /run/caddy \
    && chown 1000:1000 /etc/caddy/tls /var/lib/cseries-cloud /data/door-key /run/caddy \
    && chmod 0700 /etc/caddy/tls /var/lib/cseries-cloud \
    && chmod 0750 /data/door-key

USER 1000:1000
ENV CSERIES_DOOR_TLS=/etc/caddy/tls XDG_DATA_HOME=/run/caddy XDG_CONFIG_HOME=/run/caddy
ENTRYPOINT ["/usr/local/sbin/door-run"]
