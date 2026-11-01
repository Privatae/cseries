# cseries-browser sidecar
#
# First OCI-image sidecar in the cseries platform. Runs Chromium under
# Xvfb + Playwright + a FastAPI control API; the live viewport is a CDP
# screencast served by the control API itself (control/viewport.py) —
# the noVNC/x11vnc stack is gone. Runs on the on-demand harness lane
# (or, legacy, beside the cseries container in the entity pod).
#
# First published as v0.2.0 on 2026-08-24, carrying the /v1/sessions
# surface. The v0.1.0 shared-page-only scope described here previously
# as "image builds" never did — see the base-image note below.
#
# Image budget: ~600 MB. Lives in its own image so the entity base
# image stays trim. Pulled only by pods that have the Browser
# connection installed.

# ── The Python minor version is a CONSTRAINT, not a preference ────────
# Every native dependency pinned below tops out at cp313 wheels:
# greenlet 3.1.1 (which playwright 1.48.0 pins exactly), pydantic-core
# 2.23.4, uvloop, httptools, watchfiles. On 3.14 not one of them has a
# wheel, so pip falls back to compiling from source — and this is a slim
# image carrying neither gcc nor rust, so the build dies at the pip
# layer. That is why this image had never successfully built until
# 2026-08-24, despite the header below claiming "image builds".
#
# Do NOT resolve a future recurrence by adding build-essential. This is
# the one container whose job is rendering hostile third-party content;
# a compiler toolchain inside it is a gift to anything that gets out of
# Chromium's sandbox. Raise the floor only once the pins below publish
# wheels for the newer interpreter, and prove it before changing this
# line:
#   docker run --rm -v "$PWD/requirements.txt:/r.txt:ro" \
#     python:<candidate>-slim-bookworm sh -c \
#     "pip download --only-binary :all: --require-hashes -d /tmp/w -r /r.txt && ls /tmp/w"
# Any package that fails to resolve there = do not bump.
#
# That probe was run again on 2026-08-30, because renovate PR f3f8e920 had
# raised this line back to 3.14 on 2026-08-26 and the v0.4.1 build died at
# the same pip layer ("No such file or directory: 'gcc'" for greenlet,
# "linker `cc` not found" for pydantic-core). Measured, both bases, same
# command: 3.14 -> RESOLVE FAILED, 3.13 -> 20 wheels. renovate.json now
# holds this file's `python` below 3.14 so the bump cannot come back on
# its own; lift that rule in the same change that proves the wheels exist.
# Digest moved 2026-11-01 (fdf6f061 -> 281d39ff, Python 3.13.18): the old base carried
# openssl/libssl3 3.0.22-1~deb12u1 (CVE-2026-84782, HIGH), which apt does not upgrade
# when it installs the packages below; this one has deb12u2.
FROM python:3.13-slim-bookworm@sha256:281d39ff02e3b389401586134ff9e6572954687822a58055ec07a60ded4e0b7f

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DISPLAY=:99 \
    CONTROL_PORT=8830 \
    SCREEN_GEOMETRY=1440x900x24

# ── System packages ───────────────────────────────────────────────────
# chromium                : the browser
# chromium-sandbox        : required by chromium when run as non-root
# xvfb, x11-utils         : virtual framebuffer + xdpyinfo for readiness
# openbox                 : a window manager. Each session opens its own
#                           top-level X window; the viewport's
#                           Page.bringToFront is a window-manager raise,
#                           and an occluded window's renderer can stop
#                           producing screencast frames. ~1MB.
# fonts-*                 : minimal font set so rendering isn't garbage
# ca-certificates, curl   : TLS roots + healthcheck client
# tini                    : pid-1 reaper (clean child exits when uvicorn dies)
RUN apt-get update && apt-get install -y --no-install-recommends \
        chromium \
        chromium-sandbox \
        xvfb \
        x11-utils \
        openbox \
        fonts-liberation \
        fonts-noto-core \
        fonts-noto-cjk \
        fonts-noto-color-emoji \
        ca-certificates \
        curl \
        tini \
    && rm -rf /var/lib/apt/lists/*

# ── Python deps ───────────────────────────────────────────────────────
# Playwright (Python) drives Chromium via CDP. We use the system chromium
# package, NOT Playwright's bundled Chromium — saves ~300 MB. The
# control API picks executable_path=/usr/bin/chromium on launch.
#
# requirements.txt is the whole dependency closure, every version exact and
# every file hash-locked. Until 2026-11-01 this step ran an unpinned
# `pip install --upgrade pip` and pinned only the four direct packages, so
# starlette was whatever fastapi 0.115.0 allowed at build time: 0.38.6, below
# the fixes for CVE-2024-47874, CVE-2025-62727, CVE-2026-48818 and
# CVE-2026-54283 (1.3.1 fixes the last). fastapi 0.162.0 takes starlette 1.10.0.
#
# pip itself is pinned by hash to 26.2.2 for the install (the base image's
# 26.2.1 vendors certifi 2026.6.17, GHSA-6q48-6c7w-9gw2), then REMOVED, with
# the base image's bundled pip wheel: nothing here installs anything at run
# time, and pip vendors its own copies of urllib3, msgpack and setuptools,
# each with open HIGH findings in 26.2.2 as well. Like the compiler note
# above: this container renders hostile content, so it carries no installer.
COPY requirements.txt /tmp/requirements.txt
RUN printf '%s\n' 'pip==26.2.2 --hash=sha256:fd7f4dfa8190e014013b9f08f3e973f4e4c26ffd179a204895533a7254813a70' > /tmp/pip.txt \
    && python -m pip install --no-cache-dir --require-hashes -r /tmp/pip.txt \
    && python -m pip install --no-cache-dir --require-hashes --only-binary :all: -r /tmp/requirements.txt \
    && python -m pip uninstall -y pip \
    && rm -rf /usr/local/lib/python3.13/ensurepip/_bundled /tmp/pip.txt /tmp/requirements.txt

# ── App layout ────────────────────────────────────────────────────────
WORKDIR /app
COPY control/ /app/control/
COPY polyfill/ /app/polyfill/
COPY startup/ /app/startup/
RUN chmod +x /app/startup/entrypoint.sh /app/startup/healthcheck.sh

# Non-root user. Chromium refuses to run as root unless --no-sandbox is
# passed; we keep the sandbox + run as an unprivileged user.
RUN useradd -m -u 1000 -s /bin/bash cseries \
    && mkdir -p /home/cseries/.cache /var/lib/cseries-browser \
    && chown -R cseries:cseries /home/cseries /var/lib/cseries-browser /app
USER cseries
WORKDIR /home/cseries

EXPOSE 8830

HEALTHCHECK --interval=15s --timeout=4s --start-period=20s --retries=3 \
    CMD /app/startup/healthcheck.sh || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "/app/startup/entrypoint.sh"]
