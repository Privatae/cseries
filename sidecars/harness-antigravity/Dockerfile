# cseries-harness-antigravity — confined OCI sidecar for Antigravity CLI (`agy`),
# Google's agentic coding CLI (antigravity.google) and the vendor tool for
# Google AI Pro / Ultra accounts (Gemini CLI stopped serving those sign-ins on
# 2026-06-18 — google-gemini/gemini-cli discussion #27274).
#
# Vendor-ACCOUNT harness, like codex/grok: the user signs in to Google through
# the binary's OWN login, run by the sidecar in this pod (the host relays the
# code Google's page shows); the grant the binary writes rides back to the
# entity as rotated_creds. A Gemini API key pasted in the Keys app
# (harness-antigravity) is the second lane. See
# system-apps/_harness_runtime/specs.py (ANTIGRAVITY) for the measured
# behaviour every flag and path here rests on.
#
# The binary keeps its token file, settings, logs and conversation db under
# $HOME/.gemini/antigravity-cli, and the rootfs is read-only, so the sidecar
# points $HOME at the workspace volume — the ONLY writable path — exactly as
# kimi does with KIMI_HOME.
#
# Binary BAKED at build (no runtime download; image tag == our runtime, the
# ARGs == the vendor version inside), alongside the shared cseries_sidecar
# runtime + the shared harness handler.
#
# Build context = the cseries repo root (so COPY can reach cli/ + system-apps/):
#   docker build -f sidecars/harness-antigravity/Dockerfile \
#       -t cseries-harness-antigravity:vX.Y.Z .
#
# Confinement (applied by the operator at runtime, not here): own non-root uid,
# Localhost seccomp, drop-ALL caps, no-priv-esc, read-only rootfs, exactly two
# mounts (throwaway workspace RW + antigravity creds Secret RO).

FROM python:3.14-slim-bookworm@sha256:5127143f679a1bcf59e6b9ea1a95e6cef8b8d0905bf1ca459a7f0141398ddc4f

# VENDOR PIN — version, the release build id that is part of the artifact
# path, and the vendor's OWN published sha512 for this exact artifact. All
# three come from the release manifest the vendor's installer reads:
#   https://antigravity-cli-auto-updater-974169037036.us-central1.run.app/manifests/linux_amd64.json
#     {"version":"1.3.0",
#      "url":"https://storage.googleapis.com/antigravity-public/antigravity-cli/1.3.0-6233328509124608/linux-x64/cli_linux_x64.tar.gz",
#      "sha512":"f6f5e16f…5280"}
# (read 2026-10-06; 1.1.17-5084709148033024 was read 2026-08-21). The digest
# is verified at build, so a swapped object fails the build instead of
# shipping. Bump = re-read the manifest and move ALL THREE
# (renovate cannot follow this source; the harness-ship runbook's vendor-pin
# gate is the check). The tarball holds ONE file, `antigravity`, which the
# vendor's installer renames to `agy`; we do the same.
ARG AGY_RELEASE_VERSION=1.3.0
ARG AGY_RELEASE_BUILD=6233328509124608
ARG AGY_SHA512=f6f5e16f356ae8a345fd6ab265ad2a4e4dc8d0b7f4ac345249d0e250a06edd496677b8ac0193b258971feb7ccfbba2fccc2f482db1d62cc650498e352ef85280

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8848 \
    # Harness image: cli/cseries_sidecar.py serves /workspace/load, /workspace/export,
    # /session/export and /run/progress only when this is set (the Host image
    # never sets it, so an in-container sidecar has no such routes).
    CSERIES_SIDECAR_ROLE=harness \
    PYTHONPATH=/app/lib \
    AGY_RELEASE_VERSION=${AGY_RELEASE_VERSION} \
    # The sidecar's HOME for the vendor process — on the workspace volume (see
    # the header). The token the pod is woken with is seeded here per run.
    AGY_HOME=/var/lib/cseries/projects/.agy-home
    # NOTE: no CSERIES_SESSION_DIRS on purpose. The binary's conversation state
    # shares $HOME/.gemini/antigravity-cli with its OAuth token file. Since
    # 2026-09-22 the session export's filter does recognise
    # antigravity-oauth-token (and derived names like its .tmp) — but a filter
    # is a second line, not a reason to export a directory that holds a grant
    # at all. Session collection for this harness waits until its
    # conversation store is located under its own subdir.

# git: the in-container diff (G3c) computes it here, never host-side.
# curl/ca-certificates: download at build + healthcheck + the binary's egress
# trust root. tini: pid-1 reaper.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git ca-certificates curl tini \
    && rm -rf /var/lib/apt/lists/*

# Bake the pinned agy binary at BUILD. Dynamically linked glibc ELF (verified
# with `file` on the 1.1.17 and 1.3.0 artifacts) — bookworm carries what it needs; the
# `--version` at the end is the load-time check.
RUN mkdir -p /var/lib/cseries/apps/antigravity/bin /tmp/agy \
    && curl -fsSL "https://storage.googleapis.com/antigravity-public/antigravity-cli/${AGY_RELEASE_VERSION}-${AGY_RELEASE_BUILD}/linux-x64/cli_linux_x64.tar.gz" \
        -o /tmp/agy/cli_linux_x64.tar.gz \
    && echo "${AGY_SHA512}  /tmp/agy/cli_linux_x64.tar.gz" | sha512sum -c - \
    && tar -xzf /tmp/agy/cli_linux_x64.tar.gz -C /tmp/agy antigravity \
    && cp /tmp/agy/antigravity /var/lib/cseries/apps/antigravity/bin/agy \
    && chmod 0755 /var/lib/cseries/apps/antigravity/bin/agy \
    && printf '%s' "${AGY_RELEASE_VERSION}" > /var/lib/cseries/apps/antigravity/bin/.agy-release-tag \
    && rm -rf /tmp/agy \
    && /var/lib/cseries/apps/antigravity/bin/agy --version

# App: the shared stdlib sidecar runtime + the shared harness handler (no pip deps).
COPY cli/cseries_sidecar.py /app/lib/cseries_sidecar.py
COPY system-apps/_harness_runtime/ /app/lib/_harness_runtime/

# Confined non-root user (distinct uid per the G3 plan: antigravity=10009) +
# the workspace mountpoint the throwaway volume mounts over at runtime.
RUN useradd -m -u 10009 -s /usr/sbin/nologin harness \
    && mkdir -p /var/lib/cseries/projects \
    && chown -R harness:harness /app /var/lib/cseries/apps/antigravity /var/lib/cseries/projects
USER harness
WORKDIR /var/lib/cseries/projects
EXPOSE 8848
# Control-socket mode (bastion coder runs, no TCP port): check over the socket.
HEALTHCHECK --interval=15s --timeout=4s --start-period=10s --retries=3 \
    CMD S="${PRIVATAE_SIDECAR_CONTROL_SOCK:-$CSERIES_SIDECAR_CONTROL_SOCK}"; if [ -n "$S" ]; \
        then curl -fsS --unix-socket "$S" "http://localhost/health"; \
        else curl -fsS "http://127.0.0.1:8848/health"; fi || exit 1
ENTRYPOINT ["/usr/bin/tini", "--", "python3", "-m", "_harness_runtime", "antigravity"]
