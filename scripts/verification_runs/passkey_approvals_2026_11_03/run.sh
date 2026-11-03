#!/usr/bin/env bash
# A fresh database and one run of the browser proof (as root: it needs its own PID, network and
# mount namespaces, so a bastion or Host already on the machine is never touched).
#   run.sh <work dir> <venv with requirements.txt> <screenshot dir>
set -uo pipefail
W=${1:?work dir}; VENV=${2:?venv}; SHOTS=${3:?screenshot dir}
TREE=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)
HERE="$TREE/scripts/verification_runs/passkey_approvals_2026_11_03"
touch "$W/STOP" 2>/dev/null; for _ in $(seq 60); do grep -q stopped "$W/db.log" 2>/dev/null && break; sleep 1; done
rm -rf "$W"; mkdir -p "$W" "$SHOTS"; cd "$W"
python3 -I "$HERE/db_hold.py" "$TREE" "$W" > "$W/db.log" 2>&1 &
for _ in $(seq 120); do [ -s "$W/DSN" ] && break; sleep 1; done
timeout 900 unshare --pid --net --mount --fork --mount-proc bash "$HERE/inner.sh" "$TREE" "$W" "$VENV" "$SHOTS" > "$W/run.log" 2>&1
echo "exit=$?"; touch "$W/STOP"
