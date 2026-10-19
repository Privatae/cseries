#!/usr/bin/env bash
#
# review_path_check.sh — run the multi-harness review-path functional
# check INSIDE a live, authed C-Series and print red/green per hop.
#
# This does NOT provision anything. You bring a running, authed C-Series Host
# (a dev container, or a real pod) and
# point this at it. It copies review_path_check.py in and execs it with
# the container's own python — the faithful in-process caller position
# the entity uses. The harnesses must be CONNECTED in that environment
# (claude-code / codex authed); the smoke container won't work.
#
# Usage:
#   CSERIES_CONTAINER=cseries-dev   scripts/review_path_check.sh   # docker
#   CSERIES_POD=entity-1 CSERIES_NS=cseries \
#                               scripts/review_path_check.sh   # k8s
#
# Tunables (passed through to the python check):
#   CHECK_MAX_MINUTES (default 6)   per-harness run budget
#   CHECK_TIMEOUT_S   (default 420) how long to wait for completion
#
# Exits non-zero if any hard hop fails (WARNs do not fail).

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PY_LOCAL="$HERE/review_path_check.py"
PY_REMOTE="/tmp/review_path_check.py"

red()    { printf '\033[31m%s\033[0m\n' "$1"; }
yellow() { printf '\033[33m%s\033[0m\n' "$1"; }

[[ -f "$PY_LOCAL" ]] || { red "missing $PY_LOCAL"; exit 2; }

ENVPASS=( -e "CHECK_MAX_MINUTES=${CHECK_MAX_MINUTES:-6}" -e "CHECK_TIMEOUT_S=${CHECK_TIMEOUT_S:-420}" )

if [[ -n "${CSERIES_POD:-}" ]]; then
    NS="${CSERIES_NS:-privatae-system}"
    yellow "Target: pod $NS/$CSERIES_POD (kubectl exec)"
    kubectl -n "$NS" cp "$PY_LOCAL" "$CSERIES_POD:$PY_REMOTE"
    exec kubectl -n "$NS" exec "$CSERIES_POD" -- \
        env CHECK_MAX_MINUTES="${CHECK_MAX_MINUTES:-6}" \
            CHECK_TIMEOUT_S="${CHECK_TIMEOUT_S:-420}" \
        python3 "$PY_REMOTE"
elif [[ -n "${CSERIES_CONTAINER:-}" ]]; then
    yellow "Target: docker container $CSERIES_CONTAINER (docker exec)"
    docker cp "$PY_LOCAL" "$CSERIES_CONTAINER:$PY_REMOTE"
    exec docker exec "${ENVPASS[@]}" "$CSERIES_CONTAINER" python3 "$PY_REMOTE"
else
    red "Set CSERIES_CONTAINER=<name> (docker) or CSERIES_POD=<pod> [CSERIES_NS=<ns>] (k8s)."
    red "The target must be a running, harness-authed C-Series Host — not the smoke container."
    exit 2
fi
