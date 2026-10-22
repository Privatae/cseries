# cseries-harness-claude-code — confined OCI sidecar for claude-code as a PEER
# coding harness (G3 peer-harness model, 2026-05-30).
#
# claude-code is treated like the vendor harnesses (codex/cursor/grok):
# an ephemeral, metered, own-container, confined workload — NOT the in-process
# reasoning core (that stays as system-apps/claude-code). The Claude Agent SDK
# (and its bundled `claude` engine) is BAKED at build via pip — no runtime
# download — alongside the shared cseries_sidecar runtime + the claude-code-harness
# /run handler. Auth is OAuth ONLY (CLAUDE_CODE_OAUTH_TOKEN); no API key.
#
# Build context = the cseries repo root (so COPY can reach cli/ + system-apps/).
#   docker build -f sidecars/harness-claude-code/Dockerfile -t cseries-harness-claude-code:dev .
#
# Confinement (operator-applied at runtime): own non-root uid (10005),
# Localhost seccomp, drop-ALL caps, no-priv-esc, read-only rootfs, exactly two
# mounts (throwaway workspace RW + claude OAuth creds Secret RO). The OCI
# boundary IS the sandbox — so the SDK runs NATIVE tools, ungoverned per-call.

FROM python:3.14-slim-bookworm@sha256:5127143f679a1bcf59e6b9ea1a95e6cef8b8d0905bf1ca459a7f0141398ddc4f
# The base image's pip 26.2.1 vendors certifi 2026.6.17, whose emSign roots
# have compromised intermediate keys (GHSA-6q48-6c7w-9gw2, CRITICAL). pip
# 26.2.2 vendors the fixed 2026.10.19.
RUN printf '%s\n' 'pip==26.2.2 --hash=sha256:fd7f4dfa8190e014013b9f08f3e973f4e4c26ffd179a204895533a7254813a70' > /tmp/pip.txt \
 && python -m pip install --no-cache-dir --require-hashes -r /tmp/pip.txt \
 && rm /tmp/pip.txt

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8844 \
    # Harness image: cli/cseries_sidecar.py serves /workspace/load, /workspace/export,
    # /session/export and /run/progress only when this is set (the Host image
    # never sets it, so an in-container sidecar has no such routes).
    CSERIES_SIDECAR_ROLE=harness \
    PYTHONPATH=/app/lib

# git: the in-container diff (G3c) computes it here, never host-side.
# curl/ca-certificates: healthcheck + SDK egress. tini: pid-1 reaper.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git ca-certificates curl tini \
    && rm -rf /var/lib/apt/lists/*

# Bake the Agent SDK (+ its bundled `claude` engine) at BUILD — no runtime PyPI
# pull. The SDK reads CLAUDE_CODE_OAUTH_TOKEN and bills the user's
# subscription's Agent-SDK credit (OAuth, not the metered API).
#
# EXACT pin, as an ARG — matching every other harness (CODEX_RELEASE_TAG,
# CURSOR_RELEASE_TAG, OPENCODE_RELEASE_TAG, KIMI_RELEASE_VERSION). Until
# 2026-07-24 this was `>=0.2.87`, so the comment above it — "image tag == SDK
# version" — was simply untrue: two builds of the same source produced
# different vendor code, and a digest pin on the image could not fix that
# because the drift is INSIDE the image. Bumping this ARG is a reviewable
# commit, which is also how a new model reaches the harness.
#
# 0.2.127 (2026-07-24): bumped from the 0.2.87 floor on Opus 5 release day.
ARG CLAUDE_AGENT_SDK_VERSION=0.2.163
# sha256 of claude_agent_sdk-<version>-py3-none-manylinux_2_17_x86_64.whl, as PyPI
# publishes it. The wheel carries the bundled claude CLI, so this pins the vendor
# binary too. The SDK's own Python dependencies are still resolved by pip, unhashed.
ARG CLAUDE_AGENT_SDK_SHA256=260cb955d237b7e1897f1dbf9ee5efbf815d8622d0c7459a05409c56432d8501
RUN pip download --no-cache-dir --no-deps --only-binary=:all: -d /tmp/sdk \
        "claude-agent-sdk==${CLAUDE_AGENT_SDK_VERSION}" \
    && WHL="/tmp/sdk/claude_agent_sdk-${CLAUDE_AGENT_SDK_VERSION}-py3-none-manylinux_2_17_x86_64.whl" \
    && test "$(ls /tmp/sdk)" = "$(basename "$WHL")" \
    && echo "${CLAUDE_AGENT_SDK_SHA256}  $WHL" | sha256sum -c - \
    && pip install --no-cache-dir "$WHL" \
    && rm -rf /tmp/sdk \
    && python3 -c "import claude_agent_sdk; print('claude-agent-sdk', claude_agent_sdk.__version__ if hasattr(claude_agent_sdk,'__version__') else 'ok')"

# App: the shared stdlib sidecar runtime + the claude-code-harness handler.
COPY cli/cseries_sidecar.py /app/lib/cseries_sidecar.py
COPY system-apps/_harness_runtime/ /app/lib/_harness_runtime/
COPY system-apps/claude-code-harness/bin/main.py /app/main.py

# Confined non-root user (claude-code=10005) + workspace + home (HOME holds the
# creds copy + ~/.claude.json the SDK-driven CLI needs to init headless).
RUN useradd -m -u 10005 -s /usr/sbin/nologin -d /home/privatae harness \
    && mkdir -p /var/lib/cseries/projects /home/privatae/.claude \
    && chown -R harness:harness /app /var/lib/cseries/projects /home/privatae
USER harness
WORKDIR /var/lib/cseries/projects
EXPOSE 8844
# Control-socket mode (bastion coder runs, no TCP port): check over the socket.
HEALTHCHECK --interval=15s --timeout=4s --start-period=10s --retries=3 \
    CMD S="${PRIVATAE_SIDECAR_CONTROL_SOCK:-$CSERIES_SIDECAR_CONTROL_SOCK}"; if [ -n "$S" ]; \
        then curl -fsS --unix-socket "$S" "http://localhost/health"; \
        else curl -fsS "http://127.0.0.1:8844/health"; fi || exit 1
ENTRYPOINT ["/usr/bin/tini", "--", "python3", "/app/main.py"]
