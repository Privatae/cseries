# cseries-app-runtime-creative — the second process-app TOOLCHAIN image: the
# python toolchain (sidecars/app-runtime-python/Dockerfile) plus the creative
# programs a Host operates headless.
#
# WHY THIS EXISTS. Hosts get Blender and the ArtCraft apps (PhotoCraft,
# FilmCraft) for detailed UI and wallpaper work, in this shape: ONE
# platform-built image that carries the programs, pinned
# by sha256 here, so a store app stays a small python recipe that declares
# `runtime.image: creative` (the store distributes recipes, not
# executables). It is also the only shape that fits Blender: it is
# 1.2 GB unpacked, and a store bundle is capped at 200 MB unpacked
# (server/routes/applications.py _BUNDLE_UNPACKED_MAX). The rest of the
# ArtCraft set is included too, so a person can make everything they need.
#
# What it adds to the python toolchain, all measured in a container
# with no network, no GPU, uid 1000, read-only root:
#   blender     5.2.2 (download.blender.org, GPL-2.0-or-later). Needs the
#               X11/GL libraries below even with -b (ldd: libX11, libXrender,
#               libXfixes, libXi, libxkbcommon, libSM, libICE, libXext,
#               libGL); Mesa gives EEVEE a software OpenGL. Cycles CPU
#               rendered a 1920x1080 glass-and-metal scene at 64 samples in
#               150 s on 2 CPUs, 309 s on 1.
#   the ArtCraft programs, one per line of crafts.txt (github.com/storytold,
#               MIT or Apache-2.0; their bundled fonts are SIL OFL, and each
#               program's licence and font notices are kept in
#               /opt/craft/<name>/doc). Each `<name>-cli mcp` serves the
#               program's own MCP tools on stdio, and each links only libc,
#               libm and libgcc_s.
# Only the headless command-line programs are copied, never the desktop GUIs:
# a process app is reached over HTTP and has no display.
#
# CONTRACT. Identical to the python image's, because the operator renders the
# same three containers from whichever family the manifest names
# (the operator's APP_RUNTIME_IMAGES): python3 at
# /opt/app-runtime/{fetch,run,egress}.py, bash, curl, uid 1000, workdir
# /app-src, no ENTRYPOINT. The egress wall runs from THIS image for a creative
# app, so egress.py must be the same file the python image ships: build both
# images from the same cseries commit. scripts/test_app_runtime_creative_image.py
# holds the two Dockerfiles to the same base and the same runtime files.
#
# BUMPING a program: change its version (Blender: URL) and sha256 together
# (the checksum is the publisher's own: blender-<v>.sha256 on
# download.blender.org, the release asset digest on GitHub), rebuild, and
# re-run the proof in the ship runbook.

# ── Fetch and verify the programs (this stage is thrown away) ─────────────
FROM python:3.14-slim-bookworm@sha256:5127143f679a1bcf59e6b9ea1a95e6cef8b8d0905bf1ca459a7f0141398ddc4f AS programs

ARG BLENDER_URL=https://download.blender.org/release/Blender5.2/blender-5.2.2-linux-x64.tar.xz
ARG BLENDER_SHA256=84098912789dc450e95697c4184fb8a90acbe5111c2ba4aede3fecb57806a168

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates xz-utils \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /dl
COPY sidecars/app-runtime-creative/crafts.txt /dl/crafts.txt
RUN set -eu \
 && curl -fsSL -o blender.tar.xz "$BLENDER_URL" \
 && printf '%s  %s\n' "$BLENDER_SHA256" blender.tar.xz | sha256sum -c - \
 && mkdir -p /opt/blender \
 && tar -xJf blender.tar.xz -C /opt/blender --strip-components=1 \
 && rm blender.tar.xz
# Each ArtCraft program: fetched, checked against its pinned sha256, and only
# then unpacked; the command-line program and its licence notices are kept.
# Every step in the loop ends in `|| exit 1`: inside a loop that is part of an
# && list, sh ignores `set -e`, so a checksum that failed would otherwise go on
# to unpack the archive it just refused.
RUN set -eu \
 && grep -Ev '^[[:space:]]*(#|$)' crafts.txt > pins \
 && while read -r name ver sum; do \
      f="$name-$ver-linux-x86_64.tar.gz"; \
      curl -fsSL -o "$f" "https://github.com/storytold/$name/releases/download/v$ver/$f" || exit 1; \
      printf '%s  %s\n' "$sum" "$f" | sha256sum -c - || exit 1; \
      mkdir -p "/x/$name" "/opt/craft/$name/bin" || exit 1; \
      tar -xzf "$f" -C "/x/$name" --strip-components=1 || exit 1; \
      cp "/x/$name/bin/$name-cli" "/opt/craft/$name/bin/" || exit 1; \
      cp -r "/x/$name/share/doc/$name" "/opt/craft/$name/doc" || exit 1; \
      rm -rf "$f" "/x/$name" || exit 1; \
    done < pins \
 && test "$(ls /opt/craft | wc -l)" -eq "$(wc -l < pins)"
# Each program's NOTICE and attribution files from its source at the same tag
# (its release archive leaves them out), checked the same way.
COPY sidecars/app-runtime-creative/craft-notices.txt /dl/craft-notices.txt
RUN set -eu \
 && grep -Ev '^[[:space:]]*(#|$)' craft-notices.txt > notices \
 && while read -r name ver file sum; do \
      curl -fsSL -o "/opt/craft/$name/doc/$file" "https://raw.githubusercontent.com/storytold/$name/v$ver/$file" || exit 1; \
      printf '%s  %s\n' "$sum" "/opt/craft/$name/doc/$file" | sha256sum -c - || exit 1; \
    done < notices \
 && for d in /opt/craft/*; do test -f "$d/doc/NOTICE" || { echo "no NOTICE for $d"; exit 1; }; done

# ── The toolchain image ───────────────────────────────────────────────────
# The python toolchain, step for step (see that Dockerfile for why each line
# is there), plus the libraries Blender loads.
FROM python:3.14-slim-bookworm@sha256:5127143f679a1bcf59e6b9ea1a95e6cef8b8d0905bf1ca459a7f0141398ddc4f

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        build-essential git curl ca-certificates \
        libffi-dev libssl-dev python3-dev pkg-config \
        libx11-6 libxrender1 libxfixes3 libxi6 libxkbcommon0 libsm6 libice6 libxext6 \
        libgl1 libegl1 libgl1-mesa-dri libegl-mesa0 libglx-mesa0 \
 && rm -rf /var/lib/apt/lists/*

RUN groupadd -g 1000 privatae && useradd -u 1000 -g 1000 -m -s /bin/bash privatae \
 && mkdir -p /opt/app-runtime /app-src /var/lib/cseries/appdata \
 && chown -R 1000:1000 /app-src /var/lib/cseries/appdata

COPY --from=programs /opt/blender /opt/blender
COPY --from=programs /opt/craft /opt/craft
RUN ln -s /opt/blender/blender /usr/local/bin/blender \
 && for b in /opt/craft/*/bin/*-cli; do ln -s "$b" /usr/local/bin/; done

COPY --chown=1000:1000 sidecars/app-runtime-python/fetch.py /opt/app-runtime/fetch.py
COPY --chown=1000:1000 sidecars/app-runtime-python/run.py /opt/app-runtime/run.py
COPY --chown=1000:1000 sidecars/app-egress/egress.py /opt/app-runtime/egress.py
# The MCP server a creative store app runs for its own Host: the app's command
# is `python3 /opt/app-runtime/creative_mcp.py <program>`, so the app itself is
# a recipe with no code (see the file's own header).
COPY --chown=1000:1000 sidecars/app-runtime-creative/creative_mcp.py /opt/app-runtime/creative_mcp.py

USER 1000
WORKDIR /app-src
