# cseries-harness-kimi — confined OCI sidecar for Kimi Code, Moonshot AI's
# coding CLI (www.kimi.com/code).
#
# Like opencode this is a PARAMETERIZED lane — one image, any
# endpoint the user's credential reaches. The Keys secret 'harness-kimi' is
# either a bare vendor key (wrapped to Moonshot's API) or a JSON provider spec
# {base_url, model?, api_key?, type?}; `type` picks the wire the binary speaks
# (kimi / openai / anthropic). The sidecar hands that provider to the binary as
# the KIMI_MODEL_* env family per run (an in-memory temporary provider — the
# binary writes nothing back), so no config.toml is authored here or there and
# the API key never touches a file (system-apps/_harness_runtime/specs.py).
#
# The binary keeps sessions/logs/its stub config under $HOME/.kimi-code, and
# the rootfs is read-only, so $HOME is pointed
# at the workspace volume — the ONLY writable path.
#
# Binary BAKED at build (no runtime download; image tag == binary version),
# alongside the shared cseries_sidecar runtime + the shared harness handler.
#
# Build context = the cseries repo root (so COPY can reach cli/ + system-apps/).
#   docker build -f sidecars/harness-kimi/Dockerfile -t cseries-harness-kimi:dev .
#
# Confinement (applied by the operator at runtime, not here): own non-root uid,
# Localhost seccomp, drop-ALL caps, no-priv-esc, read-only rootfs, exactly two
# mounts (throwaway workspace RW + kimi creds Secret RO).

FROM python:3.14-slim-bookworm@sha256:5127143f679a1bcf59e6b9ea1a95e6cef8b8d0905bf1ca459a7f0141398ddc4f
# The base image's pip 26.2.1 vendors certifi 2026.6.17, whose emSign roots
# have compromised intermediate keys (GHSA-6q48-6c7w-9gw2, CRITICAL). pip
# 26.2.2 vendors the fixed 2026.10.19.
RUN printf '%s\n' 'pip==26.2.2 --hash=sha256:fd7f4dfa8190e014013b9f08f3e973f4e4c26ffd179a204895533a7254813a70' > /tmp/pip.txt \
 && python -m pip install --no-cache-dir --require-hashes -r /tmp/pip.txt \
 && rm /tmp/pip.txt

# Pinned release + the vendor's OWN published sha256 for this exact artifact,
# read from https://code.kimi.com/kimi-code/binaries/${VERSION}/manifest.json.
# The digest is verified at build, so a swapped CDN object fails the build
# instead of shipping.
ARG KIMI_RELEASE_VERSION=2.1.1
# Refreshed 2026-09-25 for 2.1.1: platforms.linux-x64.checksum in the vendor
# manifest above, and the sha256 of the downloaded binary matched it. The
# dependency bot once moved the version and left the digest behind; the
# harness auto-update job moves both. A split pair
# is caught by check_vendor_pin_pairs, and the build would die at the
# sha256sum -c below rather than ship a wrong binary.
ARG KIMI_SHA256=66f47536e40b02bb1d577cdd324768f73d39b8b9472e0ca140e73d8e225cd7de

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8847 \
    # Harness image: cli/cseries_sidecar.py serves /workspace/load, /workspace/export,
    # /session/export and /run/progress only when this is set (the Host image
    # never sets it, so an in-container sidecar has no such routes).
    CSERIES_SIDECAR_ROLE=harness \
    PYTHONPATH=/app/lib \
    KIMI_RELEASE_VERSION=${KIMI_RELEASE_VERSION} \
    # Tethers OFF by default in the image as well as in build_env — a pod must
    # never phone home, self-upgrade off a CDN rollout, or schedule work.
    # Names verified against the 0.32.0 binary.
    KIMI_CODE_NO_AUTO_UPDATE=1 \
    KIMI_DISABLE_TELEMETRY=1 \
    KIMI_DISABLE_CRON=1 \
    # Only the session store is exported for collection — deliberately NOT the
    # whole brand home. The export denylist drops credential files by NAME and
    # would not recognise kimi's own credentials/<id>.json, so the narrower dir
    # is what keeps a token from ever riding along.
    CSERIES_SESSION_DIRS=/var/lib/cseries/projects/.kimi-home/.kimi-code/sessions

# git: the in-container diff (G3c) computes it here, never host-side.
# curl/ca-certificates: download at build + healthcheck + the binary's provider
# egress trust root. tini: pid-1 reaper.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git ca-certificates curl tini \
    && rm -rf /var/lib/apt/lists/*

# Bake the pinned kimi binary at BUILD. It is a self-contained executable with
# its own bundled Node runtime, so this image needs no Node layer — the npm
# package (@moonshot-ai/kimi-code) is a plain ESM entry and would drag one in.
RUN mkdir -p /var/lib/cseries/apps/kimi/bin \
    && curl -fsSL "https://code.kimi.com/kimi-code/binaries/${KIMI_RELEASE_VERSION}/kimi-code-linux-x64" \
        -o /var/lib/cseries/apps/kimi/bin/kimi \
    && echo "${KIMI_SHA256}  /var/lib/cseries/apps/kimi/bin/kimi" | sha256sum -c - \
    && chmod 0755 /var/lib/cseries/apps/kimi/bin/kimi \
    && printf '%s' "${KIMI_RELEASE_VERSION}" > /var/lib/cseries/apps/kimi/bin/.kimi-release-tag \
    && /var/lib/cseries/apps/kimi/bin/kimi --version

# App: the shared stdlib sidecar runtime + the kimi handler (no pip deps).
COPY cli/cseries_sidecar.py /app/lib/cseries_sidecar.py
COPY system-apps/_harness_runtime/ /app/lib/_harness_runtime/

# Confined non-root user (distinct uid per the G3 plan: kimi=10008) + the
# workspace mountpoint the throwaway volume mounts over at runtime.
RUN useradd -m -u 10008 -s /usr/sbin/nologin harness \
    && mkdir -p /var/lib/cseries/projects \
    && chown -R harness:harness /app /var/lib/cseries/apps/kimi /var/lib/cseries/projects
USER harness
WORKDIR /var/lib/cseries/projects
EXPOSE 8847
# Control-socket mode (bastion coder runs, no TCP port): check over the socket.
HEALTHCHECK --interval=15s --timeout=4s --start-period=10s --retries=3 \
    CMD S="${PRIVATAE_SIDECAR_CONTROL_SOCK:-$CSERIES_SIDECAR_CONTROL_SOCK}"; if [ -n "$S" ]; \
        then curl -fsS --unix-socket "$S" "http://localhost/health"; \
        else curl -fsS "http://127.0.0.1:8847/health"; fi || exit 1
ENTRYPOINT ["/usr/bin/tini", "--", "python3", "-m", "_harness_runtime", "kimi"]
