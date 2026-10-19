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
#   docker run --rm python:<candidate>-slim-bookworm sh -c \
#     "pip download --only-binary :all: -d /tmp/w \
#        fastapi==0.115.0 'uvicorn[standard]==0.32.0' \
#        playwright==1.48.0 pydantic==2.9.2 && ls /tmp/w"
# Any package that fails to resolve there = do not bump.
#
# That probe was run again on 2026-08-30, because renovate PR f3f8e920 had
# raised this line back to 3.14 on 2026-08-26 and the v0.4.1 build died at
# the same pip layer ("No such file or directory: 'gcc'" for greenlet,
# "linker `cc` not found" for pydantic-core). Measured, both bases, same
# command: 3.14 -> RESOLVE FAILED, 3.13 -> 20 wheels. renovate.json now
# holds this file's `python` below 3.14 so the bump cannot come back on
# its own; lift that rule in the same change that proves the wheels exist.
FROM python:3.13-slim-bookworm@sha256:fdf6f061c0e3829c7ee6753d20d008e4f76d725f95af53b62bcf53b503ffb0fa

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
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir \
        fastapi==0.115.0 \
        uvicorn[standard]==0.32.0 \
        playwright==1.48.0 \
        pydantic==2.9.2

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
