# cseries-harness-codex — confined OCI sidecar for the OpenAI Codex CLI.
#
# G3 (2026-05-29): vendor coding harness as an ephemeral, metered, own-container,
# confined workload — NOT a bare process in the entity pod. The Codex binary is
# BAKED at build (no runtime download into the PVC; image tag == binary version),
# alongside the shared cseries_sidecar runtime + the codex /run handler.
#
# Build context = the cseries repo root (so COPY can reach cli/ + system-apps/).
#   docker build -f sidecars/harness-codex/Dockerfile -t cseries-harness-codex:dev .
#
# Confinement (applied by the operator at runtime, not here): own non-root uid,
# Localhost seccomp, drop-ALL caps, no-priv-esc, read-only rootfs, exactly two
# mounts (throwaway workspace RW + codex creds Secret RO). This image only bakes
# the binary + runtime; it mounts nothing sensitive of its own.

FROM python:3.14-slim-bookworm@sha256:5127143f679a1bcf59e6b9ea1a95e6cef8b8d0905bf1ca459a7f0141398ddc4f

# rust-v0.135.0 → rust-v0.149.0 (2026-08-20). The 0.135.0 binary's own model
# catalog (codex-rs/models-manager/models.json at that tag) tops out at
# gpt-5.5 / gpt-5.3-codex — it has never heard of GPT-5.6. From rust-v0.146.1
# on, the catalog carries gpt-5.6-sol (flagship, priority 1) / -terra / -luna
# and retires gpt-5.4 → terra, gpt-5.4-mini → luna. Verified against THIS
# binary on 2026-08-20: `codex login --device-auth` still prints the URL and
# the one-time code in the same two-step layout specs.py parses; `exec` still
# takes --sandbox danger-full-access / --skip-git-repo-check / --cd / --model /
# -c / --json; `--json` still emits the thread.started / item.* / turn.*
# JSONL events.py reads.
#
# rust-v0.149.0 → rust-v0.154.0 (2026-09-17), for gpt-6-astra: the catalog
# adds it as a LISTED row (`codex debug models`: GPT-6-Astra, full low→ultra
# ladder) and the CLI's no-model default moves to it — `codex exec` with no
# --model banners `model: gpt-6-astra`. Both measured on this binary; the
# gpt-5.6/5.5/5.2 rows are unchanged, so astra is the only listed-row
# difference. Re-verified on 0.154.0: `login --device-auth` still prints the
# same two-step URL + code layout, and `exec` still takes -s/--sandbox
# danger-full-access / --skip-git-repo-check / -C/--cd / -m/--model / -c /
# --json (help now lists them short-flag-first). The
# codex-code-mode-host-x86_64 asset is still published at this tag.
# Also measured inside the BUILT image on 0.154.0, unauthenticated: `-c
# model_reasoning_effort=medium` moves the banner to `reasoning effort: medium`
# (no override reads `none`), so the knob CODEX_DEFAULT_EFFORTS drives is
# intact; the dotted-path `-c mcp_servers.host.command/args` host-fetch channel
# is still accepted, with `--strict-config` rejecting a deliberately-wrong key
# (`mcp_serverz`) as the control that makes that acceptance mean something; and
# `--json` still emits thread.started / turn.started / error / item.completed
# in the shapes events.py parses.
# STILL NOT verified (needs an AUTHENTICATED run, so it belongs to the ship
# smoke): `turn.completed`'s usage field names, i.e. token accounting.
#
# rust-v0.154.0 → rust-v0.160.1 (2026-10-06, latest stable, bringing the
# harnesses up to date). Measured on THIS binary: the catalog
# (`codex debug models`, listed rows) adds gpt-6.1-sol (priority 1), gpt-6-sol
# and gpt-6-luna and DROPS gpt-5.2; the CLI's no-model default moves to
# gpt-6.1-sol (`codex exec` with no --model banners `model: gpt-6.1-sol`;
# 0.154.0 bannered gpt-6-astra, same box, same day). `exec --help` and
# `login --help` are byte-identical to 0.154.0's; `login --device-auth` still
# prints the same two-step URL + code layout (plus one new caution line after
# the code); `-c model_reasoning_effort=medium` still moves the banner to
# `reasoning effort: medium`; the dotted `-c mcp_servers.host.command/args`
# override is still accepted under --strict-config, which still rejects
# `mcp_serverz`. Inside the BUILT image, unauthenticated, `--json` emits
# thread.started / turn.started / error / item.completed / turn.failed in the
# same shapes as 0.154.0 and no code-mode-host error frame, and no process is
# left behind after the run (`features list` now carries daemon_auto_start
# stable=true). In source, exec_events.rs differs from 0.154.0 only by an
# optional `results` field on web_search items; the Usage field names are
# unchanged there — STILL not verified on a live authenticated run.
ARG CODEX_RELEASE_TAG=rust-v0.160.1
# sha256 of the two release tarballs below, as GitHub publishes them for the release's
# assets (the releases API `digest`). The build checks both, so a bump moves all three.
ARG CODEX_SHA256=9226581be592d18f7e7f740a352fdb63aa61e45e39f7eb9b09d3888c84bba33f
ARG CODEX_HOST_SHA256=8a69207d97545ac753b6585974e1e67a4c51ae5deacf06517db512bb25e0e3c2
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8840 \
    # Harness image: cli/cseries_sidecar.py serves /workspace/load, /workspace/export,
    # /session/export and /run/progress only when this is set (the Host image
    # never sets it, so an in-container sidecar has no such routes).
    CSERIES_SIDECAR_ROLE=harness \
    PYTHONPATH=/app/lib \
    CODEX_RELEASE_TAG=${CODEX_RELEASE_TAG}

# git: the in-container diff (G3c) computes it here, never host-side.
# curl/ca-certificates: download at build + healthcheck. tini: pid-1 reaper.
# ripgrep: the Codex model reaches for `rg` on its own (in one factory build
# its first command was `rg --files …` → "rg: command not found", then it fell
# back to find). Debian bookworm ships 13.0.0-4+b2.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git ca-certificates curl tini ripgrep \
    && rm -rf /var/lib/apt/lists/*

# Bake the pinned Codex binary at BUILD into the exact path bin/main.py expects
# (CACHE_DIR=/var/lib/cseries/apps/codex/bin, CODEX_BINARY=.../codex), and write the
# .codex-release-tag so _bootstrap_binary() sees {binary present + tag matches}
# and no-ops at runtime (no network egress on start).
#
# Since rust-v0.149.0 the CLI ships its "code mode" host as a SEPARATE release
# asset (codex-code-mode-host-<triple>.tar.gz) and, with the feature on by
# default (`codex features list`: code_mode_host stable true), looks for it
# beside its own binary at start. Without it every run opens with an
# item.completed/error frame — "Code Mode is unavailable because failed to
# spawn code-mode host …/codex-code-mode-host: host executable was not found"
# (measured 2026-08-20 on the bare binary) — and that frame lands in the
# user's scrollback as an error on every build. Baking the vendor's own host
# from the SAME release tag next to the binary removes it (measured: the
# error frame is gone; thread.started → turn.started). Each tarball extracts
# into its own dir so the find cannot pick the wrong member.
RUN mkdir -p /var/lib/cseries/apps/codex/bin /tmp/cx /tmp/cxh \
    && curl -fsSL "https://github.com/openai/codex/releases/download/${CODEX_RELEASE_TAG}/codex-x86_64-unknown-linux-musl.tar.gz" -o /tmp/codex.tgz \
    && echo "${CODEX_SHA256}  /tmp/codex.tgz" | sha256sum -c - \
    && tar -xzf /tmp/codex.tgz -C /tmp/cx \
    && BIN="$(find /tmp/cx -maxdepth 2 -type f -name 'codex*' ! -name '*.tgz' ! -name '*.tar.gz' | head -1)" \
    && test -n "$BIN" \
    && cp "$BIN" /var/lib/cseries/apps/codex/bin/codex \
    && chmod 0755 /var/lib/cseries/apps/codex/bin/codex \
    && curl -fsSL "https://github.com/openai/codex/releases/download/${CODEX_RELEASE_TAG}/codex-code-mode-host-x86_64-unknown-linux-musl.tar.gz" -o /tmp/codex-host.tgz \
    && echo "${CODEX_HOST_SHA256}  /tmp/codex-host.tgz" | sha256sum -c - \
    && tar -xzf /tmp/codex-host.tgz -C /tmp/cxh \
    && HOST="$(find /tmp/cxh -maxdepth 2 -type f -name 'codex-code-mode-host*' | head -1)" \
    && test -n "$HOST" \
    && cp "$HOST" /var/lib/cseries/apps/codex/bin/codex-code-mode-host \
    && chmod 0755 /var/lib/cseries/apps/codex/bin/codex-code-mode-host \
    && printf '%s' "${CODEX_RELEASE_TAG}" > /var/lib/cseries/apps/codex/bin/.codex-release-tag \
    && rm -rf /tmp/codex.tgz /tmp/codex-host.tgz /tmp/cx /tmp/cxh \
    && /var/lib/cseries/apps/codex/bin/codex --version

# App: the shared stdlib sidecar runtime + the codex handler (no pip deps).
COPY cli/cseries_sidecar.py /app/lib/cseries_sidecar.py
COPY system-apps/_harness_runtime/ /app/lib/_harness_runtime/

# Confined non-root user (distinct uid per the G3 plan: codex=10001) + the
# workspace mountpoint the throwaway PVC mounts over at runtime.
RUN useradd -m -u 10001 -s /usr/sbin/nologin harness \
    && mkdir -p /var/lib/cseries/projects \
    && chown -R harness:harness /app /var/lib/cseries/apps/codex /var/lib/cseries/projects
USER harness
WORKDIR /var/lib/cseries/projects
EXPOSE 8840
# Control-socket mode (bastion coder runs, no TCP port): check over the socket.
HEALTHCHECK --interval=15s --timeout=4s --start-period=10s --retries=3 \
    CMD S="${PRIVATAE_SIDECAR_CONTROL_SOCK:-$CSERIES_SIDECAR_CONTROL_SOCK}"; if [ -n "$S" ]; \
        then curl -fsS --unix-socket "$S" "http://localhost/health"; \
        else curl -fsS "http://127.0.0.1:8840/health"; fi || exit 1
ENTRYPOINT ["/usr/bin/tini", "--", "python3", "-m", "_harness_runtime", "codex"]
