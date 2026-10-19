#!/bin/bash
# Docker HEALTHCHECK script.
#
# Probes the control API on the loopback. Three states the
# probe distinguishes:
#   exit 0 — control API answers, returns ok=true
#   exit 1 — control API answers but reports degraded
#   exit 1 — control API unreachable (curl fails)
set -e
CONTROL_PORT="${CONTROL_PORT:-8830}"
resp=$(curl -fsS --max-time 3 "http://127.0.0.1:${CONTROL_PORT}/health" || exit 1)
echo "$resp" | grep -q '"ok": *true' || exit 1
exit 0
