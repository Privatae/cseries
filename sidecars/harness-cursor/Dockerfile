# cseries-harness-cursor — confined OCI sidecar for the Cursor CLI (cursor-agent).
#
# G3 (2026-05-29): vendor coding harness as an ephemeral, metered, own-container,
# confined workload. Cursor ships a whole dist-package/ tree (the cursor-agent
# launcher + its bundled node runtime + native .node addons + rg + sandbox + its
# bundled runtimes);
# the launcher cannot run without its siblings, so the WHOLE tree is baked.
# downloads.cursor.com 403s the default urllib User-Agent → browser UA at build.
#
# Build context = the cseries repo root:
#   docker build -f sidecars/harness-cursor/Dockerfile -t cseries-harness-cursor:dev .

FROM python:3.14-slim-bookworm@sha256:5127143f679a1bcf59e6b9ea1a95e6cef8b8d0905bf1ca459a7f0141398ddc4f

# 2026.05.28-a70ca7c → 2026.10.01-e373342 (2026-10-06, the version
# https://cursor.com/install points at). Checked on the new package: every
# flag _cursor_argv / STREAM_FLAGS pass (-p, --force, --output-format
# text|stream-json, --stream-partial-output, --model, --list-models) is in its
# --help with the same meaning; CURSOR_API_KEY is still read; the stream-json
# writer (headless.ts) emits the same frame types (one new system subtype,
# background_shell_timeout, only behind a flag the platform does not pass).
# The package is ~3x larger (557M extracted vs 178M; image ~1.86G).
ARG CURSOR_RELEASE_TAG=2026.10.01-e373342
# sha256 of agent-cli-package.tar.gz for that tag. Cursor publishes no checksum (none
# beside the package, none in cursor.com/install; checked 2026-10-06), so this is the
# hash of the download recorded when the pin moved; a re-download that differs fails here.
ARG CURSOR_SHA256=a79726c6e644520e993970be4c45775a6889802b67abe461a677a53219ae28e8
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8841 \
    # Harness image: cli/cseries_sidecar.py serves /workspace/load, /workspace/export,
    # /session/export and /run/progress only when this is set (the Host image
    # never sets it, so an in-container sidecar has no such routes).
    CSERIES_SIDECAR_ROLE=harness \
    PYTHONPATH=/app/lib \
    CURSOR_RELEASE_TAG=${CURSOR_RELEASE_TAG}

RUN apt-get update && apt-get install -y --no-install-recommends \
        git ca-certificates curl tini \
    && rm -rf /var/lib/apt/lists/*

# Bake the FULL cursor dist-package at build into the path bin/main.py expects
# (CURSOR_BIN=/var/lib/cseries/apps/cursor/bin/dist-package/cursor-agent), so
# _bootstrap_binary() sees {launcher present + executable} and no-ops at runtime.
RUN mkdir -p /var/lib/cseries/apps/cursor/bin \
    && curl -fsSL -A "Mozilla/5.0 (X11; Linux x86_64)" \
        "https://downloads.cursor.com/lab/${CURSOR_RELEASE_TAG}/linux/x64/agent-cli-package.tar.gz" \
        -o /tmp/cursor.tgz \
    && echo "${CURSOR_SHA256}  /tmp/cursor.tgz" | sha256sum -c - \
    && tar -xzf /tmp/cursor.tgz -C /var/lib/cseries/apps/cursor/bin \
    && test -f /var/lib/cseries/apps/cursor/bin/dist-package/cursor-agent \
    && chmod -R 0755 /var/lib/cseries/apps/cursor/bin/dist-package \
    && rm -f /tmp/cursor.tgz

COPY cli/cseries_sidecar.py /app/lib/cseries_sidecar.py
COPY system-apps/_harness_runtime/ /app/lib/_harness_runtime/

RUN useradd -m -u 10002 -s /usr/sbin/nologin harness \
    && mkdir -p /var/lib/cseries/projects \
    && chown -R harness:harness /app /var/lib/cseries/apps/cursor /var/lib/cseries/projects
USER harness
WORKDIR /var/lib/cseries/projects
EXPOSE 8841
# Control-socket mode (bastion coder runs, no TCP port): check over the socket.
HEALTHCHECK --interval=15s --timeout=4s --start-period=10s --retries=3 \
    CMD S="${PRIVATAE_SIDECAR_CONTROL_SOCK:-$CSERIES_SIDECAR_CONTROL_SOCK}"; if [ -n "$S" ]; \
        then curl -fsS --unix-socket "$S" "http://localhost/health"; \
        else curl -fsS "http://127.0.0.1:8841/health"; fi || exit 1
ENTRYPOINT ["/usr/bin/tini", "--", "python3", "-m", "_harness_runtime", "cursor"]
