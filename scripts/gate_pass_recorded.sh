#!/usr/bin/env bash
# gate_pass_recorded.sh <repo> — did the container-free gate list already pass
# on exactly this code? Exit 0 and print why when it did; exit 1 otherwise.
#
# WHY (measured): smoke_test.sh re-runs the shared list, 359 gates one after
# another, and that is about 37 of a 43-minute smoke. pre_merge_gates.sh
# already ran every one of them,
# 8 at a time, on the same tree minutes before, both in CI (the PR's gates job)
# and in a ship (on the clean build checkout smoke then runs from). Running them twice tells nothing new and doubled every PR and
# every ship.
#
# Two records count, and only for a CLEAN tree whose HEAD tree hash matches:
#   1. SMOKE_SHARED_GATES_PASSED_TREE, set by .gitea/workflows/check.yml on a
#      smoke job that `needs: gates`, so the gates job passed this commit in
#      this same run. That job runs PRE_MERGE_AFFECTED_ONLY=1:
#      the gates that cover the PR's change, not the whole list, which runs at
#      ship (record 2 below). The line printed says so;
#   2. $GATE_RESULTS_DIR/<tree>.pass, which pre_merge_gates.sh writes only when
#      every gate passed and the tree was clean and unchanged from start to end.
# A dirty tree never matches: the hash would not name the code that is there.
set -uo pipefail
REPO="${1:?usage: gate_pass_recorded.sh <repo>}"
tree="$(git -C "$REPO" rev-parse 'HEAD^{tree}' 2>/dev/null)" || exit 1
[[ -n "$tree" ]] || exit 1
[[ -z "$(git -C "$REPO" status --porcelain 2>/dev/null)" ]] || exit 1
if [[ -n "${SMOKE_SHARED_GATES_PASSED_TREE:-}" && "$SMOKE_SHARED_GATES_PASSED_TREE" == "$tree" ]]; then
    echo "the PR's gates job passed tree ${tree:0:12} in this CI run (the gates covering the change; the whole list runs at ship)"
    exit 0
fi
stamp="${GATE_RESULTS_DIR:-${XDG_CACHE_HOME:-${HOME:-/nonexistent}/.cache}/cseries-gate-results}/$tree.pass"
if [[ -f "$stamp" ]]; then
    echo "pre_merge_gates.sh passed tree ${tree:0:12} at $(head -c 40 "$stamp")"
    exit 0
fi
exit 1
