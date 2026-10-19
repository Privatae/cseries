# cseries-harness-grok — confined OCI sidecar for xAI's Grok CLI.
#
# G3 (2026-05-29): the 4th vendor coding harness as an ephemeral, metered,
# own-container, confined workload. The grok binary is BAKED at build (raw
# single-file binary from x.ai/cli, gcs fallback) alongside the shared
# cseries_sidecar runtime + the grok /run handler.
#
# Build context = the cseries repo root
#   docker build -f sidecars/harness-grok/Dockerfile -t cseries-harness-grok:dev .
#
# Confinement (operator-applied at runtime): own non-root uid (10004),
# Localhost seccomp, drop-ALL caps, no-priv-esc, read-only rootfs, exactly two
# mounts (throwaway workspace RW + grok creds Secret RO).

FROM python:3.14-slim-bookworm@sha256:5127143f679a1bcf59e6b9ea1a95e6cef8b8d0905bf1ca459a7f0141398ddc4f

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8843 \
    # Harness image: cli/cseries_sidecar.py serves /workspace/load, /workspace/export,
    # /session/export and /run/progress only when this is set (the Host image
    # never sets it, so an in-container sidecar has no such routes).
    CSERIES_SIDECAR_ROLE=harness \
    PYTHONPATH=/app/lib

RUN apt-get update && apt-get install -y --no-install-recommends \
        git ca-certificates curl tini \
    && rm -rf /var/lib/apt/lists/*

# Bake the grok binary at BUILD into the path bin/main.py expects
# (CACHE_DIR=/var/lib/cseries/apps/grok/bin, GROK_BIN=.../grok). Download the raw
# single-file binary (x.ai is Cloudflare-fronted → send a browser UA; fall back
# to the public GCS mirror).
#
# PINNED 2026-07-26. This used to resolve https://x.ai/cli/stable at build time,
# which meant xAI decided what shipped: two builds of the same source produced
# different vendor code, with no review and no record of what was in production.
# That is the one thing /harness-ship exists to prevent — the image digest pins
# the image, not what is inside it. Bumping this ARG is now a deliberate,
# reviewable act. (stable resolved to 0.2.112 when this was pinned; the image it
# replaced carried 0.2.51, i.e. the drift was already ~60 releases wide.)
# 1.0.46 (2026-10-06): xAI began refusing 0.2.112 with "426 Upgrade Required ...
# update to version 1.0.13 or later". Every flag
# _grok_argv passes, `login --device-auth`, the effort tiers and the auth paths
# are unchanged in 1.0.46's --help and embedded docs; its streaming-json gained
# tool_call/tool_call_update/usage/plan/available_commands (events.parse_grok).
# _bootstrap_binary() no-ops at runtime once GROK_BIN exists (no egress on start).
ARG GROK_RELEASE_TAG=1.0.46
# sha256 of grok-<tag>-linux-x86_64. xAI publishes no checksum (none beside the binary,
# none in x.ai/cli/install.sh; checked 2026-10-06), so this is the hash of the download
# recorded when the pin moved, and a re-download that differs fails the build below.
ARG GROK_SHA256=41626a53292324140b92556b9d42ff5542e3dcd04aff85eafb8689dd4adb44fc
RUN mkdir -p /var/lib/cseries/apps/grok/bin \
    && UA="Mozilla/5.0 (X11; Linux x86_64)" \
    && VER="${GROK_RELEASE_TAG}" \
    && test -n "$VER" \
    && ASSET="grok-${VER}-linux-x86_64" \
    && ( curl -fsSL -A "$UA" "https://x.ai/cli/${ASSET}" -o /var/lib/cseries/apps/grok/bin/grok \
         || curl -fsSL -A "$UA" "https://storage.googleapis.com/grok-build-public-artifacts/cli/${ASSET}" -o /var/lib/cseries/apps/grok/bin/grok ) \
    && test -s /var/lib/cseries/apps/grok/bin/grok \
    && echo "${GROK_SHA256}  /var/lib/cseries/apps/grok/bin/grok" | sha256sum -c - \
    && chmod 0755 /var/lib/cseries/apps/grok/bin/grok \
    && printf '%s' "$VER" > /var/lib/cseries/apps/grok/bin/.grok-version \
    && ( /var/lib/cseries/apps/grok/bin/grok --version || echo "grok --version non-fatal at build (may need auth)" )

# App: the shared stdlib sidecar runtime + the grok handler (no pip deps).
COPY cli/cseries_sidecar.py /app/lib/cseries_sidecar.py
COPY system-apps/_harness_runtime/ /app/lib/_harness_runtime/

# Confined non-root user (grok=10004) + the workspace mountpoint.
RUN useradd -m -u 10004 -s /usr/sbin/nologin harness \
    && mkdir -p /var/lib/cseries/projects \
    && chown -R harness:harness /app /var/lib/cseries/apps/grok /var/lib/cseries/projects
USER harness
WORKDIR /var/lib/cseries/projects
EXPOSE 8843
# Control-socket mode (bastion coder runs, no TCP port): check over the socket.
HEALTHCHECK --interval=15s --timeout=4s --start-period=10s --retries=3 \
    CMD S="${PRIVATAE_SIDECAR_CONTROL_SOCK:-$CSERIES_SIDECAR_CONTROL_SOCK}"; if [ -n "$S" ]; \
        then curl -fsS --unix-socket "$S" "http://localhost/health"; \
        else curl -fsS "http://127.0.0.1:8843/health"; fi || exit 1
ENTRYPOINT ["/usr/bin/tini", "--", "python3", "-m", "_harness_runtime", "grok"]
