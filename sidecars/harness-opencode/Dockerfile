# cseries-harness-opencode — confined OCI sidecar for the OSS opencode CLI.
#
# Workbench P6 (2026-07-02): the OPEN-SOURCE model leg. Unlike the fixed-model
# vendor harnesses (codex/cursor/grok/claude-code), opencode is
# PARAMETERIZED — one image, any model the user's OpenRouter key can reach
# (`--model openrouter/<slug>`). This is the cheap/heterogeneous proposer lane
# for Mixed-MoA and the sovereign lane's on-ramp (a bastion-GPU endpoint later
# is just a different provider slug).
#
# Binary BAKED at build (no runtime download; image tag == binary version),
# alongside the shared cseries_sidecar runtime + the opencode /run handler.
#
# Build context = the cseries repo root (so COPY can reach cli/ + system-apps/).
#   docker build -f sidecars/harness-opencode/Dockerfile -t cseries-harness-opencode:dev .
#
# Confinement (applied by the operator at runtime, not here): own non-root uid,
# Localhost seccomp, drop-ALL caps, no-priv-esc, read-only rootfs, exactly two
# mounts (throwaway workspace RW + opencode creds Secret RO).

FROM python:3.14-slim-bookworm@sha256:5127143f679a1bcf59e6b9ea1a95e6cef8b8d0905bf1ca459a7f0141398ddc4f

# v1.17.13 → v1.18.35 (2026-10-06). Checked on both binaries before pinning:
# `run --help` is byte-identical (run, -m/--model provider/model, the
# positional message, --format default|json) and OPENROUTER_API_KEY is still
# the openrouter provider's env var. At the tag the --format json writer
# (cli/cmd/run.ts), the shell tool (tool/shell.ts), the part schema and the
# database path/table opencode_running_tools reads are unchanged; a signed-out
# run prints the same error envelope. Not re-measured: a real keyed run.
# Behaviour change at v1.18.35: a permission request from a CHILD (subagent)
# session is auto-rejected like the main session's (run.ts:699-704); v1.17.13
# ignored it, so the subagent waited for an answer that never came.
ARG OPENCODE_RELEASE_TAG=v1.18.35
# sha256 of opencode-linux-x64.tar.gz, as GitHub publishes it for the release asset
# (the releases API `digest`). The build checks it, so a bump moves both.
ARG OPENCODE_SHA256=c8f888b451f5494a18f858fffb0e0b68f4e4baa9c241761c5f206884f0fa640d
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8845 \
    # Harness image: cli/cseries_sidecar.py serves /workspace/load, /workspace/export,
    # /session/export and /run/progress only when this is set (the Host image
    # never sets it, so an in-container sidecar has no such routes).
    CSERIES_SIDECAR_ROLE=harness \
    PYTHONPATH=/app/lib \
    OPENCODE_RELEASE_TAG=${OPENCODE_RELEASE_TAG} \
    CSERIES_SESSION_DIRS=/home/privatae/.local/share/opencode

# git: the in-container diff (G3c) computes it here, never host-side.
# curl/ca-certificates: download at build + healthcheck. tini: pid-1 reaper.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git ca-certificates curl tini \
    && rm -rf /var/lib/apt/lists/*

# Bake the pinned opencode binary at BUILD (github.com/anomalyco/opencode)
# into the path bin/main.py expects. GLIBC asset, deliberately NOT the -musl
# one: opencode is Bun-compiled and its musl build dynamically links
# /lib/ld-musl-x86_64.so.1, which this bookworm (glibc) base lacks — the musl
# variant exits 127 here (verified 2026-07-02).
# Extract into a SUBDIR: the tar's top-level `opencode` binary at /tmp/opencode
# collides with the /tmp/opencode temp DIR the Bun runtime creates on start —
# extracted flat, `--version` dies with EEXIST (verified 2026-07-02).
RUN mkdir -p /var/lib/cseries/apps/opencode/bin /tmp/ocx \
    && curl -fsSL "https://github.com/anomalyco/opencode/releases/download/${OPENCODE_RELEASE_TAG}/opencode-linux-x64.tar.gz" -o /tmp/opencode.tgz \
    && echo "${OPENCODE_SHA256}  /tmp/opencode.tgz" | sha256sum -c - \
    && tar -xzf /tmp/opencode.tgz -C /tmp/ocx \
    && BIN="$(find /tmp/ocx -maxdepth 2 -type f -name 'opencode*' | head -1)" \
    && test -n "$BIN" \
    && cp "$BIN" /var/lib/cseries/apps/opencode/bin/opencode \
    && chmod 0755 /var/lib/cseries/apps/opencode/bin/opencode \
    && printf '%s' "${OPENCODE_RELEASE_TAG}" > /var/lib/cseries/apps/opencode/bin/.opencode-release-tag \
    && rm -rf /tmp/opencode.tgz /tmp/ocx \
    && /var/lib/cseries/apps/opencode/bin/opencode --version

# App: the shared stdlib sidecar runtime + the opencode handler (no pip deps).
COPY cli/cseries_sidecar.py /app/lib/cseries_sidecar.py
COPY system-apps/_harness_runtime/ /app/lib/_harness_runtime/

# Confined non-root user (distinct uid per the G3 plan: opencode=10006) + the
# workspace mountpoint the throwaway volume mounts over at runtime.
RUN useradd -m -u 10006 -s /usr/sbin/nologin harness \
    && mkdir -p /var/lib/cseries/projects \
    && chown -R harness:harness /app /var/lib/cseries/apps/opencode /var/lib/cseries/projects
USER harness
WORKDIR /var/lib/cseries/projects
EXPOSE 8845
# Control-socket mode (bastion coder runs, no TCP port): check over the socket.
HEALTHCHECK --interval=15s --timeout=4s --start-period=10s --retries=3 \
    CMD S="${PRIVATAE_SIDECAR_CONTROL_SOCK:-$CSERIES_SIDECAR_CONTROL_SOCK}"; if [ -n "$S" ]; \
        then curl -fsS --unix-socket "$S" "http://localhost/health"; \
        else curl -fsS "http://127.0.0.1:8845/health"; fi || exit 1
ENTRYPOINT ["/usr/bin/tini", "--", "python3", "-m", "_harness_runtime", "opencode"]
