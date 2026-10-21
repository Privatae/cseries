# cseries-app-runtime-python — the platform's TOOLCHAIN image for a user's own
# process app ("Form 2").
#
# WHY THIS EXISTS. A Host planned a Python app with native dependencies as a
# `runtime.kind: process` app whose first boot ran apt-get, built a C library
# and made a python>=3.11 venv. That process would have been
# spawned INSIDE the entity container (uid 1000, no sudo, no gcc/make, only
# the kernel's python 3.14) and the only runtime-pod image was a scratch Go
# binary — so a real project cloned from GitHub could run nowhere on the
# platform, while the workflow reported "open the live app".
#
# This image is what `runtime.image: python` in an app.yaml resolves to
# (cseries server/applications/manifest.py ALLOWED_RUNTIME_IMAGES; the operator
# pins it by digest in APP_RUNTIME_IMAGES). It is a toolchain, not an app:
# the app's install dir is fetched into the pod at start by the init step
# below, the manifest `command` runs in it as uid 1000, and everything the
# app installs (a venv, a checkout, data) goes under /var/lib/cseries/appdata/
# <app_id>/ — a per-app PVC the operator mounts, so it survives sleeps.
#
# Contract the operator renders against (kopf_handlers _reconcile_app_runtime,
# form=process):
#   init:  /opt/app-runtime/fetch.py  — GET $CSERIES_KERNEL_URL/api/applications/
#          $PRIVATAE_APP_ID/source with X-App-Runtime-Source-Token, extract to
#          $APP_SRC (emptyDir, the working dir of the main container).
#   main:  the manifest command, cwd $APP_SRC, env PORT/PRIVATAE_APP_ID/
#          CSERIES_KERNEL_URL + the node-squid proxy vars.
#   probe: curl against 127.0.0.1:$PORT$READY_PATH (see the operator).
#
# No root at runtime and no apt for the app: the toolchain the app needs is
# HERE. An app that needs a system library this image lacks is a request to
# extend THIS image (a platform change), never a reason to run as root.

# Same base family every harness sidecar pins (python:*-slim-bookworm by
# digest), and the same VERSION as the kernel and the harnesses: 3.14.
# Wheel availability for real-world projects is no reason to pin older
# (checked against PyPI): numpy, pandas, scipy, TA-Lib, cffi and pydantic-core all publish a literal
# cp314-cp314-manylinux x86_64 wheel. cryptography is the one to state
# precisely: its linux x86_64 wheels are cp39-abi3 / cp311-abi3 plus a
# cp314-cp314t (free-threaded) build, so on the standard-GIL 3.14 in this image
# pip installs the abi3 wheel — a binary wheel, no rust toolchain needed, but
# NOT a cp314-tagged one. Do not restate this as "cryptography ships cp314".
#
# Note what the version is and is not. Nothing but pip itself is installed in this
# Dockerfile, so the base version is never a build risk for this image — it is
# a RUNTIME contract for the user's app, which resolves its own wheels at
# first boot against whatever interpreter is here. That is also why the
# browser sidecar's `allowedVersions: "<3.14"` ceiling in renovate.json does
# not apply to this file: there the 3.14 base is unbuildable at the pip layer,
# here there is no pip layer to break.
#
# Re-pin by digest when bumping. This digest is the same 3.14-slim-bookworm
# manifest list every harness sidecar already pins.
FROM python:3.14-slim-bookworm@sha256:5127143f679a1bcf59e6b9ea1a95e6cef8b8d0905bf1ca459a7f0141398ddc4f

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# pip 26.2.1, the one the base image carries, vendors certifi 2026.6.17, whose
# CA bundle trusts the emSign root behind eight intermediates with compromised
# keys (GHSA-6q48-6c7w-9gw2, CRITICAL). pip 26.2.2 vendors the fixed 2026.10.19.
# Pinned by hash; drop this once the base image ships 26.2.2 or later.
RUN printf '%s\n' 'pip==26.2.2 --hash=sha256:fd7f4dfa8190e014013b9f08f3e973f4e4c26ffd179a204895533a7254813a70' > /tmp/pip.txt \
 && python -m pip install --no-cache-dir --require-hashes -r /tmp/pip.txt \
 && rm /tmp/pip.txt

# The toolchain: compiler + headers for native wheels (TA-Lib, cffi,
# cryptography), git for `pip install git+…` / clones, curl for the probe
# and for scripts that fetch, python venv module (in the base image).
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        build-essential git curl ca-certificates \
        libffi-dev libssl-dev python3-dev pkg-config \
 && rm -rf /var/lib/apt/lists/*

# uid 1000 = `privatae`, the same uid the entity's appdata and every harness
# pod use, so a PVC written here is readable by the kernel's tooling later.
RUN groupadd -g 1000 privatae && useradd -u 1000 -g 1000 -m -s /bin/bash privatae \
 && mkdir -p /opt/app-runtime /app-src /var/lib/cseries/appdata \
 && chown -R 1000:1000 /app-src /var/lib/cseries/appdata

COPY --chown=1000:1000 sidecars/app-runtime-python/fetch.py /opt/app-runtime/fetch.py
# The main container's wrapper: the operator renders the manifest
# command as `python3 /opt/app-runtime/run.py -- <command>`; run.py tees what
# the app prints to its kernel (POST /api/applications/<id>/runtime-log with
# the same source token fetch.py presents) so the host can read a failed boot.
COPY --chown=1000:1000 sidecars/app-runtime-python/run.py /opt/app-runtime/run.py
# The egress wall. Ships in THIS image so the sidecar container
# needs no second image and no second pull: the operator runs the same image
# with a different command. See sidecars/app-egress/egress.py — it makes the
# app's declared network[] hosts actually resolvable and reachable, and
# nothing else. stdlib only; it imports nothing from the toolchain.
COPY --chown=1000:1000 sidecars/app-egress/egress.py /opt/app-runtime/egress.py

USER 1000
WORKDIR /app-src
# No ENTRYPOINT: the operator renders the manifest `command` verbatim (and the
# init container's fetch), the same way the helper's config drives it.
