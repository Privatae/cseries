#!/bin/bash
# entrypoint.sh — boot the browser sidecar.
#
# Order: Xvfb (display) → openbox (window manager) → uvicorn (control
# API). Each one waits until the previous is healthy. Failures are
# loud — we don't want a half-up sidecar swallowing errors.
#
# The x11vnc → websockify → noVNC chain that used to sit between
# openbox and uvicorn is GONE. The viewport is now a CDP screencast
# served by the control API itself (control/viewport.py) on the one
# control port — no second port, no desktop capture, nothing extra to
# expose. The harness Service only ever routed the control port, so
# the VNC lane was unreachable in production anyway.
set -euo pipefail

DISPLAY="${DISPLAY:-:99}"
SCREEN_GEOMETRY="${SCREEN_GEOMETRY:-1440x900x24}"
CONTROL_PORT="${CONTROL_PORT:-8830}"

log() { printf '[entrypoint] %s\n' "$*" >&2; }

# 0. Clean up any stale display state from a previous run.
# `docker restart` (or k8s container restart in-place) leaves the
# Xvfb lock + socket on disk and Xvfb refuses to bind ('Server is
# already active for display 99'). Removing them is safe — pid 1
# is fresh and no Xvfb is running.
DISPLAY_NUM="${DISPLAY#:}"
rm -f "/tmp/.X${DISPLAY_NUM}-lock" "/tmp/.X11-unix/X${DISPLAY_NUM}" 2>/dev/null || true

# 1. Xvfb on $DISPLAY
log "starting Xvfb on $DISPLAY ($SCREEN_GEOMETRY)"
Xvfb "$DISPLAY" -screen 0 "$SCREEN_GEOMETRY" -nolisten tcp -ac &
XVFB_PID=$!

# Wait for Xvfb to be ready (xdpyinfo returns 0 when the display answers).
for i in $(seq 1 30); do
    if xdpyinfo -display "$DISPLAY" >/dev/null 2>&1; then
        log "Xvfb ready after ${i}x0.2s"
        break
    fi
    sleep 0.2
    if [ "$i" = "30" ]; then
        log "FATAL: Xvfb never came up"
        exit 1
    fi
done

# 1b. openbox — the window manager.
#
# Still needed with the VNC stack gone: each browser session opens its
# own top-level X window, and the viewport raises the viewed page with
# Page.bringToFront when the user switches to it. Raising is a window-
# manager operation, so without one the request is a no-op and an
# occluded window's renderer can stop producing screencast frames.
log "starting openbox on $DISPLAY"
openbox --config-file /app/startup/openbox-rc.xml >/tmp/openbox.log 2>&1 &
OPENBOX_PID=$!
# It only needs to own the root window before the first Chromium starts.
for i in $(seq 1 25); do
    if xprop -root _NET_SUPPORTING_WM_CHECK >/dev/null 2>&1; then
        log "openbox ready after ${i}x0.2s"
        break
    fi
    sleep 0.2
done

# 2. Control API on $CONTROL_PORT
#    LOOPBACK ONLY. The control API has no authentication of its own, and
#    since /v1/sessions it can drive allow-listed sites using stored
#    logins — a much bigger prize than the screenshot-one-page surface it
#    started as. It was binding 0.0.0.0, which put it on the pod IP and
#    reachable by anything that could route there. The only legitimate
#    caller is the cseries container, which shares this pod's network
#    namespace and reaches us on 127.0.0.1 (see README "Ports"), so
#    loopback costs nothing and removes the pod-external surface
#    entirely. Override only with a deliberate reason.
CONTROL_BIND="${CONTROL_BIND:-127.0.0.1}"
log "starting control API on $CONTROL_BIND:$CONTROL_PORT"
cd /app
exec python3 -m uvicorn control.app:app \
    --host "$CONTROL_BIND" \
    --port "$CONTROL_PORT" \
    --log-level info
