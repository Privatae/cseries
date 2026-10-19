#!/usr/bin/env bash
# smoke_reap_leftovers.sh — remove the containers and volumes an earlier smoke
# run left when it died before its EXIT trap (a cancelled CI job, a timeout, a
# kill). Run by smoke_test.sh before it starts its own.
#
# WHY (measured 2026-10-06): cancelled CI runs' smoke pods (app + Postgres) were
# still up 13 minutes, 52 minutes and 3 days later, and nothing removed them.
#
# THE RULE is kernel_schema.py's (maker_gone; scripts/gate_maker.sh is its shell
# copy). Every smoke container and volume carries the labels of the run that
# made it (smoke_test.sh: gate_maker_args smoke), and one is removed only when
# that run is gone: kill -0 finds no such process, /proc shows a zombie, or
# /proc shows its pid started at another time (a newer process has the pid).
# A run /proc does not show is NOT gone. The CI runner runs three jobs at once
# under ProtectProc=invisible, so one job's /proc hides another job's
# processes, and this script judged a run alive only by a readable
# /proc/<pid>/cmdline. Measured: one CI run's smoke job, starting beside
# another's, removed that run's live pod and both its volumes ("removed
# container cseries-smoke-949159, left by smoke run 949159", 20:29:41Z), and that
# run then failed on "No such container: cseries-smoke-949159". A container or volume
# without the labels (made before them, or by anything else) is never touched.
# Containers go first, so the volumes they held go in the same pass; a volume a
# container still uses stays.
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/gate_maker.sh"

_maker='{{.Label "cseries.gate.pid"}}|{{.Label "cseries.gate.pidns"}}|{{.Label "cseries.gate.pidstart"}}|{{.Label "cseries.gate.boot"}}'
docker ps -a --filter "label=$GATE_MAKER_LABEL=smoke" --format "{{.Names}}|$_maker" 2>/dev/null \
  | while IFS='|' read -r _c _pid _ns _start _boot; do
        gate_maker_gone "$_pid" "$_ns" "$_start" "$_boot" || continue
        echo "removing container $_c: the smoke run that made it, pid $_pid, is gone: $GATE_MAKER_WHY"
        docker rm -f "$_c" >/dev/null 2>&1 || echo "  docker rm -f $_c failed"
    done
docker volume ls --filter "label=$GATE_MAKER_LABEL=smoke" --format "{{.Name}}|$_maker" 2>/dev/null \
  | while IFS='|' read -r _v _pid _ns _start _boot; do
        gate_maker_gone "$_pid" "$_ns" "$_start" "$_boot" || continue
        [ -z "$(docker ps -a -q --filter "volume=$_v")" ] || continue
        echo "removing volume $_v: the smoke run that made it, pid $_pid, is gone: $GATE_MAKER_WHY"
        docker volume rm "$_v" >/dev/null 2>&1 || echo "  docker volume rm $_v failed"
    done
exit 0
