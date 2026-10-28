#!/usr/bin/env bash
# pre_merge_gates.sh — every smoke gate that needs no container, run against the
# WORKING TREE, in seconds.
#
# WHY THIS EXISTS (measured on one ship).
# Smoke found four real defects on that ship. Every gate that caught one already
# existed and did its job — the cost was WHEN they ran. scripts/smoke_test.sh
# only runs inside the image build-and-publish step, which means after a merge and after a ~4-minute
# image build. So each defect cost a full build+smoke cycle to surface, and that
# ship burned five of them.
#
# Three of those four were caught by gates that need NOTHING but the source tree:
#   - test_bundle_freshness.py  (stale workbench bundle — the old UI would have
#     shipped; this is the failure mode that once dropped 6 days of UI)
#   - check_tool_lanes.py       (a new tool registered in 3 of the 5 required
#     places, so it resolved as retired)
#   - test_phase_spec_gate.py   (which was not wired into smoke at all until
#     that session — it existed and never ran)
# The fourth (test_factory_build_gate.py) genuinely needs the container: it
# drives the build door against a live DB. It is correctly not here.
#
# HOW THE LIST IS BUILT — derived, never hand-maintained.
# The gate list is parsed out of smoke_test.sh itself, by finding the gates smoke
# invokes as `python3 "$SMOKE_REPO/scripts/X.py"` — i.e. against the source tree
# rather than via `docker exec`. A hardcoded copy of that list would drift from
# smoke the first time someone added a gate; this cannot. Add a container-free
# gate to smoke and it appears here automatically.
#
# PURE_DESPITE_DOCKER_EXEC below is the one hand-maintained part, and it is
# deliberately tiny: gates smoke happens to invoke via `docker exec` that were
# MEASURED to run correctly from the tree. Verify before adding one —
# `APP_ROOT=<repo> python3 scripts/<gate>.py` must pass on a clean tree — and say
# in the comment when you measured it. A gate that needs a live DB, a live entity,
# or kubectl does NOT belong here; wiring one of those into a lane that cannot run
# it is its own failure (test_research_paths.py red-failed a ship exactly that
# way).
#
# USAGE
#   scripts/pre_merge_gates.sh              # this repo
#   scripts/pre_merge_gates.sh /path/to/repo
# Exit 0 = every container-free gate passes. Non-zero = the count that failed.
#
# WHEN A GATE NEEDS DOCKER (test_restore_carry_real_schema.py is one: it runs a
# real Postgres in a container of its own). Docker must be reachable from where
# this runs: `docker version` must answer, and DOCKER_HOST is honoured (the CI
# runner uses a rootless daemon). A gate that cannot reach it exits 3 and is
# counted as a FAILURE, never skipped: start the daemon, or point DOCKER_HOST
# at one, and run again. Such a gate removes only the containers and images it
# labelled itself, and it needs room for its image under the docker root, so
# check `df` there before a run on a nearly full disk.
#
# This does NOT replace smoke. It moves the cheap half earlier. The container
# half still has to run at ship time.
set -uo pipefail

REPO="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
SMOKE="$REPO/scripts/smoke_test.sh"
# A gate's CPU budget in seconds (scripts/hang_watch.py): over it, it is a
# runaway. Not wall-clock time; a slow, loaded box never spends it.
PER_GATE_TIMEOUT="${PRE_MERGE_GATE_TIMEOUT:-180}"

if [[ ! -f "$SMOKE" ]]; then
    echo "pre_merge_gates: no scripts/smoke_test.sh under $REPO" >&2
    exit 2
fi

red()   { printf '\033[31m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }
dim()   { printf '\033[2m%s\033[0m\n' "$*"; }

# Gates smoke runs straight off the tree.
mapfile -t DERIVED < <(grep -oP 'python3 "\$SMOKE_REPO/scripts/\K[a-z_0-9]+\.py' "$SMOKE" | sort -u)

# Gates smoke runs via `docker exec` that were measured to run fine from the tree.
#   check_tool_lanes.py — verified from the tree: reads the
#   registry modules only (public_tool_registry / code_exec_guard / tool_access),
#   no DB, no container. Passed in seconds with APP_ROOT set.
#   test_untrusted_content_screen.py — verified from the tree: reads tools.py
#   and review.py as source and imports
#   untrusted_content/input_guard only; no DB, no container. With APP_ROOT set it
#   reproduced smoke's two failures exactly, in under a second, and passes
#   on the fix. It ran only in smoke until then, so the cap batch merged red.
#   test_host_build_door.py — verified from the tree: inspects session/meta-tool
#   source and builds a prompt; no DB,
#   no container. With APP_ROOT set it reproduced smoke's three failures exactly
#   (a change rewrote the read branch its regex anchors on) and passes on the fix.
#   It ran only in smoke, so that change merged with it red and the ship found it.
#   test_secret_shapes.py — verified from the tree: imports
#   secret_shapes, server.vault.providers and runtime/session only; no DB, no
#   container; passes from the tree in under a second. Run from the tree it
#   also checks the two composers' TS scanners against the pod's on every
#   section-13 case (section 14, needs node; in-container the web sources are
#   absent and it skips), which is the only place that check can run.
#   test_agent_grant_wall.py — verified from the tree: imports the mastercode
#   runtime modules only
#   (meta_tool_caps, agent_grant, agent_dispatch, scoped_agent, tools); no DB,
#   no container; passes from the tree in under a second. The VS Code removal dropped
#   `editor` from META_TOOL_CAPS but left its READ_SAFE_OPS row, and this gate
#   died on KeyError: 'editor' from the tree exactly as smoke would have; it
#   ran only in smoke, so the full container-free gate was green on it.
#   test_governance_floor.py — verified from the tree: runs the real
#   EntityRuntime.send_message with a scripted model and a
#   stub pool (the harness of test_late_nudge_once.py, itself container-free)
#   and reads the kit's notice from the tree; no DB, no container. 17 of its
#   checks fail on a tree where the referral still replaced the Host's reply, so
#   the no-hijack doctrine is guarded at merge, not first at the ship.
PURE_DESPITE_DOCKER_EXEC=(check_tool_lanes.py test_untrusted_content_screen.py test_host_build_door.py
                          test_secret_shapes.py test_agent_grant_wall.py test_governance_floor.py)

# The shared list, also read by smoke_test.sh — one entry gates a merge AND a
# ship. Everything in it was once running nowhere at all; see the
# header of scripts/gates-container-free.txt. scripts/test_gate_coverage.py
# enforces that both readers still consume it, so this cannot quietly become a
# one-sided list. One file per gate in gates-container-free.d/, so
# two branches that each add a gate never conflict; the .txt is read too, and
# the coverage gate keeps it free of entries.
SHARED_LIST="$REPO/scripts/gates-container-free.txt"
SHARED_DIR="$REPO/scripts/gates-container-free.d"
SHARED=()
if [[ -d "$SHARED_DIR" ]]; then
    while IFS= read -r line; do
        line="${line%%#*}"; line="$(printf '%s' "$line" | tr -d '[:space:]')"
        [[ -n "$line" ]] && SHARED+=("$line")
    # A glob kept by `-f`, which follows a symlink as the coverage gate's
    # os.path.isfile does: `find -type f` skipped a symlinked gate file the
    # coverage gate counted, so that gate ran nowhere.
    done < <(for _f in "$SHARED_LIST" "$SHARED_DIR"/*.txt; do [[ -f "$_f" ]] && awk 1 "$_f"; done)
else
    red "pre_merge_gates: scripts/gates-container-free.d/ is missing — the gates"
    red "  it carries are not running here. Restore it rather than ignoring this."
fi

GATES=()
for g in "${DERIVED[@]}" "${SHARED[@]}" "${PURE_DESPITE_DOCKER_EXEC[@]}"; do
    [[ -f "$REPO/scripts/$g" ]] && GATES+=("$g")
done
# de-dup, keep order stable
mapfile -t GATES < <(printf '%s\n' "${GATES[@]}" | awk '!seen[$0]++')

# Guard on DERIVED, not GATES. GATES also holds the shared list and
# check_tool_lanes.py, so it can never reach 0 and a check on it is decoration:
# if smoke's invocation pattern changes and the grep above stops matching, the
# suite silently drops the 36 derived gates and passes green on the remaining 19.
# Measured on a stub tree with a non-matching smoke_test.sh: it ran 19 gates,
# announced nothing, and would have exited 0 in a tree where those 19 pass. That
# is the exact "cannot drift" claim in this file's header, falsified.
if [[ ${#DERIVED[@]} -eq 0 ]]; then
    echo "pre_merge_gates: parsed 0 gates out of smoke_test.sh — its invocation" >&2
    echo "  pattern changed and the grep above no longer matches. Fix the grep;" >&2
    echo "  do NOT ship the green partial run this would otherwise produce." >&2
    exit 2
fi

# ── Run them in PARALLEL ──────────────────────────────────────────────────
# Measured before this: 108s wall for 110 gates, and the per-gate durations
# summed to 108s — strictly sequential, one interpreter per gate, no overlap.
# Three gates were 63 of those seconds. The gates are already independent
# processes, so a job pool is a runner change, not a gate change.
#
# What makes it safe: every gate that listens binds port 0 (ephemeral) and
# uses tempfile for its scratch — swept — EXCEPT the one below,
# which binds a fixed unix socket and therefore runs alone after the pool
# drains. Add to EXCLUSIVE anything that ever takes a fixed path or port.
#
# Longest gates start first so the pool's tail is short: wall time is then
# bounded by the longest single gate, not by the sum. The order comes from the
# last recorded run (scripts/gate_timing.py, below); SLOW_FIRST is only the
# order among gates that have no recorded time yet.
JOBS="${PRE_MERGE_JOBS:-$(nproc 2>/dev/null || echo 4)}"
[[ "$JOBS" =~ ^[0-9]+$ ]] && (( JOBS >= 1 )) || JOBS=1   # 0 or junk would spin on `wait -n`
EXCLUSIVE=()
SLOW_FIRST=(test_sidecar_workspace_routes_harness_only.py
            test_harness_run_liveness.py test_backup_ship.py test_loop_splay.py
            test_resident_lifecycle.py test_harness_runtime.py
            test_app_delivery_proven.py)

# Gates that need more than PER_GATE_TIMEOUT, with the measurement for each:
# scripts/gate-timeouts.txt, which smoke_test.sh reads too (there still as
# wall-clock seconds; here as each gate's CPU budget).
declare -A GATE_TIMEOUT=()
if [[ -f "$REPO/scripts/gate-timeouts.txt" ]]; then
    # `|| [[ -n ... ]]`: a last line with no trailing newline is still read,
    # as smoke_test.sh's awk reads it.
    while read -r _gt_name _gt_s _ || [[ -n "$_gt_name" ]]; do
        [[ -z "$_gt_name" || "$_gt_name" == \#* ]] && continue
        [[ "$_gt_s" =~ ^[0-9]+$ ]] && GATE_TIMEOUT["$_gt_name"]="$_gt_s"
    done < "$REPO/scripts/gate-timeouts.txt"
fi
timeout_for() {
    local t="${GATE_TIMEOUT[$1]:-}"
    if [[ -n "$t" ]] && (( t > PER_GATE_TIMEOUT )); then echo "$t"; else echo "$PER_GATE_TIMEOUT"; fi
}

_in() { local n="$1"; shift; local x; for x in "$@"; do [[ "$x" == "$n" ]] && return 0; done; return 1; }

# ── A store app's gates run on their own lane ────────────────────────────────
# Store apps are not kernel code, so their gates do not belong in a kernel run.
# One CI run took 19 min because the Telegram store app's 9-minute gate
# was picked as covering a kernel change, failed in the pool and ran alone for
# 9 more. scripts/gates-store-apps.txt names every gate whose subject is a store
# app; neither this run (a PR's nor the ship's full one) nor smoke's
# container-free pass runs one. scripts/store_app_gates.sh runs them, through
# this script with PRE_MERGE_STORE_GATES set (the same pool, per-gate limits,
# hang_watch and retry alone); its tests/ entries this run's pytest leaves out.
STORE_GATES=(); TESTS_IGNORE=()
if [[ -f "$REPO/scripts/gates-store-apps.txt" ]]; then
    # Read as store_app_gates.sh and test_gate_coverage.py read it: '#' starts
    # a comment anywhere, and a line counts only with a gate AND an app, since
    # the store lane runs no other line.
    while IFS= read -r _sl || [[ -n "$_sl" ]]; do
        _sl="${_sl%$'\r'}"; _sl="${_sl%%#*}"
        read -r _sg _sa _ <<<"$_sl"
        [[ -n "$_sg" && -n "${_sa:-}" ]] || continue
        # Only ever one regular test file: a stray `tests/` or `tests/sub/` line
        # would --ignore a directory, the whole lane at worst.
        # test_gate_coverage.py refuses such a line; this keeps it harmless.
        if [[ "$_sg" == tests/* ]]; then
            [[ "$_sg" == *.py && "$_sg" != */../* && -f "$REPO/$_sg" ]] && TESTS_IGNORE+=("--ignore=$_sg")
        else
            STORE_GATES+=("$_sg")
        fi
    done < "$REPO/scripts/gates-store-apps.txt"
fi
STORE_MODE=""
if [[ -n "${PRE_MERGE_STORE_GATES:-}" ]]; then
    STORE_MODE=1
    read -ra _want <<<"$PRE_MERGE_STORE_GATES"
    GATES=()
    for g in "${_want[@]}"; do
        if ! _in "$g" "${STORE_GATES[@]}" || [[ ! -f "$REPO/scripts/$g" ]]; then
            echo "pre_merge_gates: $g is not a store-app gate in scripts/gates-store-apps.txt" >&2
            exit 2
        fi
        _in "$g" "${GATES[@]}" || GATES+=("$g")
    done
else
    _kernel=()
    for g in "${GATES[@]}"; do _in "$g" "${STORE_GATES[@]}" || _kernel+=("$g"); done
    GATES=("${_kernel[@]}")
fi

POOL=(); ALONE=()
for g in "${SLOW_FIRST[@]}"; do _in "$g" "${GATES[@]}" && ! _in "$g" "${EXCLUSIVE[@]}" && POOL+=("$g"); done
for g in "${GATES[@]}"; do
    _in "$g" "${POOL[@]}" && continue
    if _in "$g" "${EXCLUSIVE[@]}"; then ALONE+=("$g"); else POOL+=("$g"); fi
done

if [[ -n "$STORE_MODE" ]]; then
    echo "store-app gates — ${#GATES[@]} gates of store apps against $REPO"
else
    echo "pre-merge gates — ${#GATES[@]} container-free gates against $REPO"
fi
echo "  ${JOBS} in parallel; ${#ALONE[@]} run alone afterwards (${ALONE[*]:-none})"
echo

TMPD="$(mktemp -d)"
trap 'rm -rf "$TMPD"' EXIT

# Every gate's tempfile scratch lands under $TMPD, so the trap above reaps it.
# Measured on the build machine: gates mkdtemp and never remove, one run left
# 54 dirs in /tmp, and ~1,400 runs over about three weeks held ~520k of the 1M
# inodes on the /tmp tmpfs; every shell on the box then failed with ENOSPC.
# A gate that hardcodes a /tmp path (EXCLUSIVE above) is unaffected.
mkdir -p "$TMPD/scratch"
export TMPDIR="$TMPD/scratch"

# ── What every gate cost, and the order the next run starts them in ─────────
# (scripts/gate_timing.py has the measurement.) Each gate and lane
# appends one row to $TMPD/timings.tsv; at the end the run prints how it could
# have been faster and records its times for the next run. The history lives in
# the first of these that this user can write: $GATE_RESULTS_DIR (default
# cseries-gate-results under the user's cache directory), then that cache
# directory itself.
# Timing never fails a run: without the tool or a writable directory, the gates
# run in list order and the report says what it could not compare.
TIMING="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/gate_timing.py"
TIMINGS_DIR=""
for _td in "${GATE_RESULTS_DIR:-${XDG_CACHE_HOME:-${HOME:-/nonexistent}/.cache}/cseries-gate-results}" \
           "${XDG_CACHE_HOME:-${HOME:-/nonexistent}/.cache}/cseries-gate-results"; do
    if mkdir -p "$_td" 2>/dev/null && [[ -w "$_td" ]]; then TIMINGS_DIR="$_td"; break; fi
done
: >"$TMPD/timings.tsv"
# Read once, at the start: a run that finishes meanwhile changes the file, not
# this run's order or its comparison.
[[ -n "$TIMINGS_DIR" ]] && cp "$TIMINGS_DIR/timings-latest.tsv" "$TMPD/history.tsv" 2>/dev/null
time_row() {   # name start rc kind [times [end]] — one line, appended (atomic under PIPE_BUF)
    printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$1" "$2" "${6:-$EPOCHREALTIME}" "$3" "$4" "${5:-}" >>"$TMPD/timings.tsv"
}
timing_report() {
    [[ -f "$TIMING" ]] || return 0
    python3 "$TIMING" report --run "$TMPD/timings.tsv" --history "$TMPD/history.tsv" \
        --jobs "$JOBS" --started "$run_started" --dir "${TIMINGS_DIR:-}" 2>/dev/null || true
    [[ -n "$TIMINGS_DIR" ]] && python3 "$TIMING" record --run "$TMPD/timings.tsv" \
        --dir "$TIMINGS_DIR" ${START_TREE:+--tree "$START_TREE"} >/dev/null 2>&1
    return 0
}

run_gate() {
    # Output to a file, never the terminal: the report below prints every
    # gate in the same stable order as before, so a green run reads the same
    # whether it ran on one core or sixteen.
    #
    # No 180 s wall-clock limit: it was a performance budget,
    # and a loaded box failed gates that were only slow. hang_watch stops a
    # gate that hangs idle (its processes spend no CPU for 120 s), runs away
    # (more CPU than its budget: timeout_for, the measured per-gate figure,
    # read as CPU seconds), or never ends (a 1800 s BACKSTOP, six times the
    # slowest gate at load 71, for pollers that spend a little CPU forever).
    # It writes the gate's stacks into its output first, then exits 124 with
    # the reason on the last "hang_watch:" line.
    local g="$1" t0=$SECONDS t_start="$EPOCHREALTIME"
    # CSERIES_REQUIRE_NODE: this lane has node (the apps are built with it), so
    # a gate that reads the apps' TS/JS through it FAILS here when node is
    # missing instead of skipping — a skip would read as a pass in this
    # summary. On a laptop with no node those gates still skip, loudly.
    # `times` after the gate: the CPU everything it ran spent, for the timing
    # report (scripts/gate_timing.py).
    (cd "$REPO" && APP_ROOT="$REPO" CSERIES_REQUIRE_NODE=1 python3 "$REPO/scripts/hang_watch.py" \
        --cpu-budget "$(timeout_for "$g")" ${HANG_WATCH_LOG:+--log "$HANG_WATCH_LOG.$g"} -- \
        python3 "$REPO/scripts/$g" >"$TMPD/$g.out" 2>&1
     _rc=$?; times >"$TMPD/$g.times" 2>/dev/null; exit $_rc)
    local rc=$?
    echo "$rc" >"$TMPD/$g.rc"
    echo $((SECONDS - t0)) >"$TMPD/$g.dur"
    time_row "$g" "$t_start" "$rc" "${2:-gate}" "$(tr '\n' ' ' <"$TMPD/$g.times" 2>/dev/null)"
    # Said the moment it happens, on stderr, so a session watching the log
    # sees a failure in the first minute instead of at the end; the report
    # below still prints every gate in its stable order.
    [[ "$rc" == 0 ]] || red "FAIL  $g (exit $rc, $((SECONDS - t0))s) — details in the report at the end" >&2
}

# ── The gates that cover the change run FIRST ────────────────────────────────
# Measured: one PR's full run (~15 min) was started six times and one
# counted; two of those runs failed on gates that read the changed files, and
# their failures were only printed at the end. So: the gates whose source names
# a changed file (its path, its file name, or its module path), the changed
# gate files themselves, the whole-tree gates (below) and the gates that failed
# on this tree last time run first. If any of them fails the run stops there
# and lists every failure, in a minute or two rather than fifteen. PRE_MERGE_FULL=1 runs everything
# regardless. Results live in $GATE_RESULTS_DIR (last failures per tree, and a
# pass per code tree hash, which scripts/gate_pass_recorded.sh reads so the
# same code is not run twice); a directory that cannot be written is skipped.
RESULTS_DIR="${GATE_RESULTS_DIR:-${XDG_CACHE_HOME:-${HOME:-/nonexistent}/.cache}/cseries-gate-results}"
mkdir -p "$RESULTS_DIR" 2>/dev/null || RESULTS_DIR=""
REPO_KEY="$(cd "$REPO" && pwd -P | sha1sum | cut -c1-16)"
LAST_FAILED="${RESULTS_DIR:+$RESULTS_DIR/last-failed-$REPO_KEY}"
# A store-app run neither reads nor writes the kernel lane's last failures.
[[ -n "$STORE_MODE" ]] && LAST_FAILED=""
# The code this run tests, taken BEFORE any gate runs and only on a clean tree.
# A pass is recorded at the end only if the tree is still clean and still this
# tree: a commit made while the run was going would otherwise get the pass,
# though the gates that ran before it read other code.
START_TREE=""
if [[ -n "$RESULTS_DIR" && -z "$STORE_MODE" ]] && _st="$(git -C "$REPO" status --porcelain 2>/dev/null)" && [[ -z "$_st" ]]; then
    START_TREE="$(git -C "$REPO" rev-parse 'HEAD^{tree}' 2>/dev/null || true)"
fi
CHANGED=()
first_base="$(git -C "$REPO" merge-base HEAD origin/main 2>/dev/null || true)"
if [[ -n "$first_base" ]]; then
    # -z, as the undefined-names lane below: without it git C-quotes a
    # non-ASCII path ("caf\303\251.py") and no gate's source names that form.
    # --no-renames: a rename is listed by its old path too, so a gate still
    # reading the old path runs first.
    mapfile -d '' -t CHANGED < <({ git -C "$REPO" diff -z --name-only --no-renames "$first_base"
                                   git -C "$REPO" ls-files -z -o --exclude-standard; } 2>/dev/null | sort -zu)
fi
# ── A script that reads a path the change touched counts as changed ─────────
# A helper the gates import can read a whole directory: scripts/kernel_schema.py
# builds the schema from every migrations/v*.sql, and 75 gates build on it, so
# a new migration turns them red while naming none of them. Such a script says
# what it reads, one line each, at the start of the line:
#     # gate-reads: migrations/ — what it reads there
# (a path ending in / is everything under it; any other path is that one file),
# and a change there counts as a change to the script itself: the gates that
# import it, or name it, run first.
READS_CHANGED=()
while IFS= read -r _decl; do
    _rf="${_decl%%:# gate-reads: *}"; _rp="${_decl#*:# gate-reads: }"; _rp="${_rp%%[[:space:]]*}"
    [[ -n "$_rp" ]] || continue
    for f in "${CHANGED[@]}"; do
        if [[ "$f" == "$_rp" || ( "$_rp" == */ && "$f" == "$_rp"* ) ]]; then
            _in "scripts/${_rf##*/}" "${CHANGED[@]}" "${READS_CHANGED[@]}" || READS_CHANGED+=("scripts/${_rf##*/}")
            break
        fi
    done
done < <(grep -H -E '^# gate-reads: [^[:space:]]' "$REPO"/scripts/*.py 2>/dev/null)
FIRST_PATTERNS="$TMPD/first-patterns"
# How a gate imports a changed module, which names none of its path, file name
# or dotted module: `from server.routes import vault` (one line, or inside
# parentheses over several), `from kernel_schema import ...`, `import
# kernel_schema as ks` (scripts/ is on a gate's path, as brain/ is). Extended
# regexes, matched over the whole file (grep -z) so a parenthesised list
# spanning lines is one match.
IMPORT_PATTERNS="$TMPD/import-patterns"
: >"$FIRST_PATTERNS"; : >"$IMPORT_PATTERNS"
for f in "${CHANGED[@]}" "${READS_CHANGED[@]}"; do
    [[ -n "$f" && "$f" != *$'\n'* ]] || continue   # a pattern file is one per line
    printf '%s\n' "$f" >>"$FIRST_PATTERNS"
    b="$(basename "$f")"
    case "$b" in
        __init__.py|app.yaml|index.html|package.json|package-lock.json|README.md|.web-src-rev) ;;
        *) printf '%s\n' "$b" >>"$FIRST_PATTERNS" ;;
    esac
    if [[ "$f" == *.py ]]; then
        mod="${f%.py}"; mod="${mod#brain/}"
        printf '%s\n' "${mod//\//.}" >>"$FIRST_PATTERNS"
        imp="${mod#scripts/}"; imp="${imp//\//.}"; imp="${imp%.__init__}"
        [[ "$imp" =~ ^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$ && "$imp" != __init__ ]] || continue
        leaf="${imp##*.}"
        if [[ "$imp" == *.* ]]; then
            pkg="${imp%.*}"; pkg="${pkg//./\\.}"
            printf '%s\n' \
                "from[[:blank:]]+$pkg[[:blank:]]+import[[:blank:]]+([^)#[:cntrl:]]*[^[:alnum:]_[:cntrl:]])?$leaf([^[:alnum:]_]|\$)" \
                "from[[:blank:]]+$pkg[[:blank:]]+import[[:blank:]]*\\([^)]*[^[:alnum:]_]$leaf[^[:alnum:]_]" >>"$IMPORT_PATTERNS"
        else
            printf '%s\n' "(^|[^[:alnum:]_.])(import|from)[[:blank:]]+$leaf([^[:alnum:]_.]|\$)" \
                "import_module\\([\"']$leaf[\"']\\)" >>"$IMPORT_PATTERNS"
        fi
    fi
done
covers() {   # gate — does its source name, or import, something this change touched?
    _in "scripts/$1" "${CHANGED[@]}" "${READS_CHANGED[@]}" && return 0
    [[ -s "$FIRST_PATTERNS" ]] && grep -qF -f "$FIRST_PATTERNS" "$REPO/scripts/$1" 2>/dev/null && return 0
    [[ -s "$IMPORT_PATTERNS" ]] && grep -qzE -f "$IMPORT_PATTERNS" "$REPO/scripts/$1" 2>/dev/null && return 0
    return 1
}
declare -A FIRST=()
for g in "${POOL[@]}"; do covers "$g" && FIRST["$g"]=1; done
# ── Whole-tree gates cover every change ──────────────────────────────────────
# A gate whose subject is the whole tree or a whole table (every tracked file,
# every /api route in the built app, every tool in the registry, every
# migration, every app manifest) finds what it checks by walking, globbing or
# importing, so its source names none of the files that can turn it red, and
# the match above never picks it. Measured: a PR added two /api routes with no
# route_policy.py entry and merged green, because test_api_route_guards.py
# names neither route file; a PR put a private document's name in a comment
# and merged green, because test_public_source_clean.py (every tracked file)
# names no file at all; each failed only later, at the full gate or the
# publish. Such a gate says so on a line of its own source, at the start of the
# line:
#     # gate-scope: whole-tree — what it reads, in a few words
# and then covers every change: it runs first, and on every pull request
# (PRE_MERGE_AFFECTED_ONLY), whatever the change touched. Only a gate this run
# would run anyway counts (a store app's gate stays on its own lane).
# scripts/test_whole_tree_gates.py pins this on a planted change no gate names.
# A line that is plainly meant as one but is not in that form (indented, a
# different case or spacing, another dash, another scope) still selects its
# gate, since a typo must never drop a gate, and is named here;
# test_whole_tree_gates.py, which runs on every pull request, fails on it.
WHOLE_TREE_RE='^# gate-scope: whole-tree([[:space:]]|$)'
SCOPE_ANY_RE='^[[:space:]]*#+[[:space:]]*gate[-_[:space:]]*scope[[:space:]]*:'
WHOLE_TREE=()
for g in "${GATES[@]}"; do
    if grep -qE "$WHOLE_TREE_RE" "$REPO/scripts/$g" 2>/dev/null; then
        WHOLE_TREE+=("$g")
    elif grep -qiE "$SCOPE_ANY_RE" "$REPO/scripts/$g" 2>/dev/null; then
        WHOLE_TREE+=("$g")
        red "pre_merge_gates: scripts/$g has a gate-scope line not in the form '# gate-scope: whole-tree — ...';" >&2
        red "  it runs as whole-tree, and scripts/test_whole_tree_gates.py fails until the line is fixed" >&2
    fi
done
for g in "${POOL[@]}"; do _in "$g" "${WHOLE_TREE[@]}" && FIRST["$g"]=1; done
if [[ -n "$LAST_FAILED" && -f "$LAST_FAILED" ]]; then
    while read -r g; do _in "$g" "${POOL[@]}" && FIRST["$g"]=1; done <"$LAST_FAILED"
fi
# Longest first, from the last recorded run; gates with no recorded time first
# of all, since nothing says they are short. Kept only if it is the same set of
# gates, so a broken tool can reorder nothing away.
if [[ -f "$TIMING" ]] && (( ${#POOL[@]} )); then
    mapfile -t _ordered < <(printf '%s\n' "${POOL[@]}" | python3 "$TIMING" order --history "$TMPD/history.tsv" 2>/dev/null)
    if [[ "$(printf '%s\n' "${_ordered[@]}" | sort)" == "$(printf '%s\n' "${POOL[@]}" | sort)" ]]; then
        POOL=("${_ordered[@]}")
    fi
fi
STAGE1=(); STAGE2=()
for g in "${POOL[@]}"; do
    if [[ -n "${FIRST[$g]:-}" && -z "$STORE_MODE" ]]; then STAGE1+=("$g"); else STAGE2+=("$g"); fi
done

# ── PRE_MERGE_AFFECTED_ONLY=1: a pull request runs what covers it ──
# CI ran every gate on every push to every PR. Measured on the forge for
# this repo: 613 test scripts (24 three months earlier); in one week, 272 runs,
# median 12 min, p90 50 min, 68% failed, many on gates the PR never touched. With this set, a run keeps the
# gates that cover the change (FIRST above: their source names a changed
# path, file name or module, they are a changed gate, or they are whole-tree),
# the alone-gates that do, the tests/ lane and the undefined-names lane, and
# drops the rest. The ship still runs everything: its own run of this script has no such env, and
# no pass is recorded here, because a .pass names a FULL run and smoke's skip
# (scripts/gate_pass_recorded.sh) relies on that. With no origin/main to diff
# against, nothing is known to be unaffected, so everything runs.
AFFECTED_ONLY=""
if [[ -n "${PRE_MERGE_AFFECTED_ONLY:-}" && -z "${PRE_MERGE_FULL:-}" && -z "$STORE_MODE" ]]; then
    if [[ -n "$first_base" ]]; then
        AFFECTED_ONLY=1
        _alone=()
        for g in "${ALONE[@]}"; do
            if _in "$g" "${WHOLE_TREE[@]}" || covers "$g"; then
                _alone+=("$g")
            fi
        done
        echo "affected-only: ${#STAGE1[@]} pooled + ${#_alone[@]} alone of ${#GATES[@]} gates cover this change"
        echo "  (${#WHOLE_TREE[@]} of them whole-tree, which cover every change);"
        echo "  the other $(( ${#GATES[@]} - ${#STAGE1[@]} - ${#_alone[@]} )) run at ship (PRE_MERGE_FULL=1 runs them here)"
        echo
        STAGE2=(); ALONE=("${_alone[@]}")
        GATES=("${STAGE1[@]}" "${ALONE[@]}")
    else
        red "affected-only asked for, but there is no origin/main to diff against: running every gate"
    fi
fi

# ── tests/ too runs when the change can reach it (PRE_MERGE_AFFECTED_ONLY) ────
# Measured: tests/ was 363 s of a 364 s gates job for a change of two
# docs/wiki files, which nothing in tests/ reads. scripts/ci_plan.py says what
# the lane reads (tests/, pytest's config, the image's files, the scripts/
# modules tests/ imports, every repo path those name) and whether this change
# touched any of it. Only an affected-only run asks, so a release or a ship
# (PRE_MERGE_FULL, or no PRE_MERGE_AFFECTED_ONLY) always runs tests/; a plan
# that fails or answers anything but "not needed" runs it too.
TESTS_SKIP_WHY=""
if [[ -n "$AFFECTED_ONLY" && -f "$REPO/scripts/ci_plan.py" ]]; then
    printf '%s\0' "${CHANGED[@]}" >"$TMPD/changed"
    _why="$(python3 "$REPO/scripts/ci_plan.py" --repo "$REPO" --lane tests --changed-list "$TMPD/changed" 2>&1)"
    _rc=$?
    if (( _rc == 1 )) && [[ -n "$_why" ]]; then
        TESTS_SKIP_WHY="$_why"
        echo "affected-only: tests/ not run: $TESTS_SKIP_WHY (scripts/ci_plan.py)"
        echo
    elif (( _rc != 0 )); then
        red "affected-only: scripts/ci_plan.py could not say what tests/ reads (exit $_rc); running tests/" >&2
    fi
fi

# PRE_MERGE_SELECT_ONLY=1: say which gates this run would run, one
# `would run: <gate>` line each, and run nothing (scripts/test_whole_tree_gates.py
# reads the real tree's selection this way). Exit 2, as every other run that
# tested nothing: a caller that reads the exit status (CI, store_app_gates.sh,
# a release run in a shell where this was left exported) must never read a run
# that ran no gate as a pass.
if [[ -n "${PRE_MERGE_SELECT_ONLY:-}" ]]; then
    printf 'would run: %s\n' "${STAGE1[@]}" "${STAGE2[@]}" "${ALONE[@]}" | grep -v '^would run: $'
    echo "pre_merge_gates: PRE_MERGE_SELECT_ONLY is set: no gate ran, so this run is not a pass" >&2
    exit 2
fi

run_pool() {
    # Waits on ITS OWN gates only. The tests/ lane is a background job of this
    # shell too (started beside the gates); a bare `wait` here would block on it
    # and reap it, losing its exit status, and it does not count toward JOBS.
    # Only the gates STILL RUNNING go to `wait -n`: bash keeps a reaped job's
    # status, so `wait -n` on a list holding one returns at once, and the loop
    # spun on fork+grep for the rest of the pool (measured: 537
    # iterations in 1.5 s of a toy pool). That is a core taken from the
    # load-sensitive gates.
    local g p live pids=() running=()
    for g in "$@"; do
        while :; do
            live=$'\n'"$(jobs -rp)"$'\n'; running=()
            for p in "${pids[@]}"; do [[ "$live" == *$'\n'"$p"$'\n'* ]] && running+=("$p"); done
            (( ${#running[@]} >= JOBS )) || break
            wait -n "${running[@]}" 2>/dev/null || true
        done
        run_gate "$g" &
        pids+=("$!")
    done
    (( ${#pids[@]} )) && wait "${pids[@]}"
    return 0
}

# Gates whose verdict rests on the wall-clock or CPU timing of real processes
# (the harness watchdogs). Inside the full pool they can miss a margin they hold
# on their own: measured, each failed one full run on a tree that did
# not touch it and passed alone on the same tree. One that fails in the pool
# runs ONCE more alone, after the pool; a real regression fails alone too. The
# report names every gate that needed it, so a run that leaned on this says so.
#
# The Telegram and Slack connector example gates
# run a real connector against the real routes for 5 to 10 minutes and wait
# on it with fixed windows (Telegram's check P waits 20 s for three reports).
# One CI run failed test_connector_telegram_example.py
# in the pool twice: check L the first time, then L
# and P on the rerun. Alone on the same tree it passed 127 of
# 127. Another run failed
# test_connector_slack_example.py's check L in the pool, the only failure of
# 464. The pool got heavier: two new gates each
# seed about 100k turn rows (test_project_list_speed_pg.py and
# test_workbench_index_pg.py). Checks L and P stay as strict as they
# are; this only gives either gate its one retry alone.
#
# The sidecar workspace-routes gate reads the
# longest stall of a 5 ms ticker on the event loop while each adversarial
# payload is collected, and fails any over 100 ms. One CI run
# failed collect_limits_hostile in the pool with a 152 ms stall; its
# second attempt on the same head passed. Alone on main
# the gate passed all 464 checks in 56 s, that case at an 11.7 ms
# stall; beside 24 busy loops on the build machine it passed again, that case at 36.8 ms,
# so the stall grows with the machine's load. Loops alone did not reach 152 ms.
# The 100 ms budget stays as it is; this only gives the gate its one retry alone.
#
# The book of pages' browser gate drives the built
# Workbench in headless Chromium and runs for every PR. One CI run
# failed three of its checks in the pool:
# reduced motion's crossfade, a hold read MAX where HIGH was due, and
# a finger lifted sent max where high was due. Beside 24 busy loops
# that head failed 2 of 4 runs. Those three checks now run on the page's own
# clock (page.clock) and passed beside the same loops, but the gate's other
# scenarios still settle on about 40 fixed wait_for_timeout sleeps that no one
# has audited for load. Every check stays as strict as it is; this only gives
# the gate its one retry alone.
LOAD_SENSITIVE=(test_harness_requested_step_window.py test_harness_grok_command_matrix.py
                test_connector_telegram_example.py test_connector_slack_example.py
                test_sidecar_workspace_routes_harness_only.py test_workbench_book.py
                test_workbench_book_index.py test_workbench_book_colours.py)
retried_names=()
retry_alone() {
    local g
    for g in "$@"; do
        _in "$g" "${LOAD_SENSITIVE[@]}" || continue
        [[ "$(cat "$TMPD/$g.rc" 2>/dev/null)" == 0 ]] && continue
        [[ -f "$TMPD/$g.pool_rc" ]] && continue
        cp "$TMPD/$g.rc" "$TMPD/$g.pool_rc" 2>/dev/null || echo 1 >"$TMPD/$g.pool_rc"
        cp "$TMPD/$g.out" "$TMPD/$g.pool.out" 2>/dev/null || true
        run_gate "$g" alone-retry
        retried_names+=("$g")
    done
}

failed=0
failed_names=()
report() {
    local g rc dur out last
    for g in "$@"; do
    rc="$(cat "$TMPD/$g.rc" 2>/dev/null || echo 1)"
    [[ -n "$rc" ]] || rc=1       # an EMPTY rc file is not a pass: `[[ "" -eq 0 ]]` is true in bash
    dur="$(cat "$TMPD/$g.dur" 2>/dev/null || echo 0)"
    out="$(cat "$TMPD/$g.out" 2>/dev/null)"
    last="$(printf '%s' "$out" | tail -1)"
    if [[ "$rc" == 0 ]]; then
        green "OK    $g (${dur}s)"
        if [[ -f "$TMPD/$g.pool_rc" ]]; then
            dim "      failed in the parallel pool (exit $(cat "$TMPD/$g.pool_rc")), passed alone on its retry:"
            printf '%s\n' "$(cat "$TMPD/$g.pool.out" 2>/dev/null)" | grep -E 'FAIL' | tail -3 | sed 's/^/        /'
        fi
    elif [[ $rc -eq 124 ]]; then
        red   "FAIL  $g — stopped by hang_watch after ${dur}s"
        red   "      $(printf '%s\n' "$out" | grep -E '^hang_watch: ' | tail -1 | sed 's/^hang_watch: stopped [^—]*— //')"
        printf '%s\n' "$out" | grep -vE '^hang_watch: ' | tail -25 | sed 's/^/      /'
        failed=$((failed + 1)); failed_names+=("$g")
    else
        red   "FAIL  $g (exit $rc, ${dur}s)"
        # The failed checks and errors first, when the tail below does not hold
        # them. A gate that prints more after a failed check (psql notices from
        # the next migration, a teardown) pushes the check out of its last 20
        # lines; in one CI run test_project_list_speed_pg.py's report was
        # notices only, and the check that failed could not be read.
        local tail20
        tail20="$(printf '%s\n' "$out" | tail -20)"
        printf '%s\n' "$out" \
            | grep -E '^[[:space:]]*FAIL([[:space:]]|:)|^[A-Za-z_.]*(Error|Exception): ' \
            | grep -vxF -f <(printf '%s\n' "$tail20") | head -8 | sed 's/^/      /'
        printf '%s\n' "$tail20" | sed 's/^/      /'
        failed=$((failed + 1)); failed_names+=("$g")
    fi
    [[ -n "$last" ]] && [[ $rc -eq 0 ]] && dim "      $last"
    done
}

record_failures() {
    [[ -n "$LAST_FAILED" ]] || return 0
    printf '%s\n' "${failed_names[@]}" >"$LAST_FAILED" 2>/dev/null || true
}

started=$SECONDS
run_started="$EPOCHREALTIME"

# ── tests/ starts NOW, beside the gates ──────────────────────────────────────
# It used to start after the last gate, so its whole run was added to the wall
# time: measured in one CI run, 13 gates in 274 s, then tests/ and the
# undefined-names lane took the run to 555 s. It reads nothing the gates write,
# so it runs in its own process group from here and is collected below where it
# used to run. The background process IS pytest_supervisor.py (the subshell
# execs it), so any exit, an early stop included, sends it SIGTERM and waits:
# it then stops pytest its own orderly way (pytest runs in its own session,
# which a signal to this script's process group would not reach).
#
# The lane writes its own end time ($TMPD/tests.end) the moment pytest exits:
# collected after the last gate, its row used to end there, so one CI run's
# report read "then one at a time: tests/ 1102s" for a pytest that took 311 s
# beside the gates. The subshell no longer execs the supervisor, so it passes a
# TERM on to it and waits, which keeps the orderly stop described above.
TESTS_DIR="$REPO/tests"
TESTS_PID=""
if [[ -z "$STORE_MODE" && -z "$TESTS_SKIP_WHY" && -d "$TESTS_DIR" ]] && [[ -n "$(find "$TESTS_DIR" -name 'test_*.py' -print -quit 2>/dev/null)" ]]; then
    TESTS_BACKSTOP="$(timeout_for tests/)"
    _t_start="$EPOCHREALTIME"
    ( cd "$REPO" || exit 1
      export CSERIES_REQUIRE_CHROMIUM=1
      trap '[[ -n "${_sup:-}" ]] && { kill -TERM "$_sup" 2>/dev/null; wait "$_sup" 2>/dev/null; }; exit 143' TERM
      python3 scripts/pytest_supervisor.py --backstop-s "$TESTS_BACKSTOP" -- \
          python3 -m pytest tests/ -q -p no:cacheprovider "${TESTS_IGNORE[@]}" &
      _sup=$!
      wait "$_sup"; _rc=$?
      echo "$EPOCHREALTIME" >"$TMPD/tests.end"
      exit "$_rc" ) >"$TMPD/tests.out" 2>&1 &
    TESTS_PID=$!
    trap '[[ -n "$TESTS_PID" ]] && { kill -TERM "$TESTS_PID" 2>/dev/null; wait "$TESTS_PID" 2>/dev/null; }; rm -rf "$TMPD"' EXIT
fi

if (( ${#STAGE1[@]} )); then
    echo "first: ${#STAGE1[@]} gates that cover this change or failed here last time"
    run_pool "${STAGE1[@]}"
    retry_alone "${STAGE1[@]}"
    early=0
    for g in "${STAGE1[@]}"; do [[ "$(cat "$TMPD/$g.rc" 2>/dev/null)" == 0 ]] || early=$((early + 1)); done
    if (( early )) && [[ -z "${PRE_MERGE_FULL:-}" ]]; then
        echo
        report "${STAGE1[@]}"
        record_failures
        timing_report
        echo
        red "PRE-MERGE GATES FAILED EARLY — $failed of the ${#STAGE1[@]} gates covering this change ($((SECONDS - started))s)"
        for n in "${failed_names[@]}"; do red "  - $n"; done
        red "The other $(( ${#STAGE2[@]} + ${#ALONE[@]} )) gates did not run. Fix every one listed, run the"
        red "gates for the files you touched, commit, then run this once more."
        red "(PRE_MERGE_FULL=1 runs everything regardless.)"
        exit 1
    fi
    dim "  all ${#STAGE1[@]} passed in $((SECONDS - started))s; running the other $(( ${#STAGE2[@]} + ${#ALONE[@]} ))"
    echo
fi
run_pool "${STAGE2[@]}"
retry_alone "${STAGE2[@]}"
for g in "${ALONE[@]}"; do run_gate "$g" alone; done

report "${GATES[@]}"

# ── tests/ — the OTHER suite, which ran nowhere at all ───────────────────────
#
# scripts/ has 323 gate files and a coverage ratchet forcing every one into a
# lane. tests/ has 26 files and 519 tests, and it was once invoked
# by NOTHING: not smoke, not this script, not either gates list, and there is no
# CI on this repo. Ten of those tests were failing and nobody could have known.
#
# Eight were one defect: test_browser_recorder.py read the process-wide event
# loop, and asyncio.run() leaves that UNSET on 3.14 — so the files sorting
# before it (test_autofill_*, test_automated, test_bastion_*) wiped the loop it
# had set at import. All 15 passed when the file was run alone, which is exactly
# why it survived. The other two asserted doctrine phrases in a tool description
# that e3233b12 had deliberately moved into a skill.
#
# It costs ~17s, which roughly doubles this script's runtime and is still
# seconds. scripts/test_gate_coverage.py asserts this block still exists, so
# tests/ cannot quietly fall back out of the lane the way it was never in one.
# (TESTS_DIR is set, and the lane started, above the gates; this block collects it.)
# RECURSIVE, not compgen on the top level. A `tests/unit/` reorganisation would
# otherwise take this whole block out silently — measured: moving the 26 files
# one directory down ran the lane in 15s with no tests/ line at all, and the
# ratchet skipped its check too, so BOTH halves went quiet together while the
# banner still said "+ tests/". That is the green-partial-run shape the
# DERIVED -eq 0 guard above exists to prevent, one directory over.
TESTS_RAN=0
if [[ -n "$TESTS_PID" ]]; then
    echo
    # CSERIES_REQUIRE_CHROMIUM: this lane has a browser, so a Chromium-driven
    # test that cannot launch one FAILS here instead of skipping (a skip reads
    # as a pass in this summary). tests/conftest.py turns those skips into
    # failures; on a laptop with no browser they still skip.
    # scripts/pytest_supervisor.py runs the lane: a separate process that
    # fails a test still running after 300 s (with every thread's stack),
    # stops a run stuck collecting or inside a call that never returns, and
    # stops the whole run at its BACKSTOP. The backstop is the lane's own line
    # in scripts/gate-timeouts.txt (`tests/`), read like a gate's limit; it is
    # a hang backstop, not a speed budget (a speed budget went red at load
    # average ~50). Every verdict is printed on its own output, which pytest
    # does not capture. (The command itself is launched above the gates.)
    wait "$TESTS_PID"; rc=$?
    TESTS_PID=""
    out="$(cat "$TMPD/tests.out" 2>/dev/null)"
    # Its own end, not now: it ran beside the gates (see where it starts).
    _t_end="$(cat "$TMPD/tests.end" 2>/dev/null)"; [[ -n "$_t_end" ]] || _t_end="$EPOCHREALTIME"
    tests_s=$(( ${_t_end%%[.,]*} - ${_t_start%%[.,]*} ))
    time_row "tests/" "$_t_start" "$rc" beside "" "$_t_end"
    TESTS_RAN=1
    if [[ $rc -eq 0 ]]; then
        green "OK    tests/ (${tests_s}s, beside the gates)"
        dim   "      $(printf '%s\n' "$out" | grep -E '[0-9]+ (passed|failed)' | tail -1)"
    elif [[ $rc -eq 124 ]]; then
        # Its own branch, like the gates above: the supervisor stopped the run.
        # Its verdict says where (a test that would not stop, collecting, or
        # the ${TESTS_BACKSTOP}s backstop), and the stacks follow it.
        red   "FAIL  tests/ — stopped by pytest_supervisor (${tests_s}s)"
        printf '%s\n' "$out" | sed -n '/^== pytest_supervisor ==$/,$p' | head -60 | sed 's/^/      /'
        failed=$((failed + 1)); failed_names+=("tests/")
    else
        red   "FAIL  tests/ (exit $rc, ${tests_s}s)"
        printf '%s\n' "$out" | grep -E '^(FAILED|ERROR) ' | head -20 | sed 's/^/      /'
        printf '%s\n' "$out" | sed -n '/^== pytest_supervisor ==$/,$p' | head -60 | sed 's/^/      /'
        printf '%s\n' "$out" | tail -5 | sed 's/^/      /'
        failed=$((failed + 1)); failed_names+=("tests/")
    fi
elif [[ -n "$TESTS_SKIP_WHY" ]]; then
    echo
    dim "SKIP  tests/ — not needed by this change: $TESTS_SKIP_WHY"
fi

# ── undefined names — the NameError no other gate reads for ──────────────────
#
# A change once used DCN_DISPATCH_TIMEOUT without defining it, and passed every
# gate above: a gate only fails on a name if it happens to execute that line, and
# nothing did. pyflakes resolves names statically, so this lane needs no test that
# reaches the code.
#
# It fails ONLY on the undefined-name class: "undefined name X", "undefined name
# X in __all__", and "local variable X ... referenced before assignment" — and
# on a file pyflakes cannot read at all (a syntax error), since none of its
# names were checked. Unused
# imports, unused locals, f-strings without placeholders and the rest of what
# pyflakes says are NOT failures — the tree has ~400 of them, and a gate that
# fails on style gets bypassed, which loses this one with it.
#
# Scope: the .py files changed since the merge base with origin/main (committed,
# staged, unstaged and untracked); CI fetches main as origin/main for this (and
# for PRE_MERGE_AFFECTED_ONLY). Where there is no merge base it checks every
# tracked .py file, which is clean (8s) and must stay clean. pyflakes is pinned in
# scripts/requirements-gates.txt (pip install -r it); PRE_MERGE_PYFLAKES overrides
# the command. A missing pyflakes FAILS this lane — it never skips, since a skip
# reads as a pass in this summary.
UNDEF_RAN=0
if [[ -z "$STORE_MODE" ]] && git -C "$REPO" rev-parse --git-dir >/dev/null 2>&1; then
    UNDEF_RAN=1
    t0=$SECONDS
    _t_start="$EPOCHREALTIME"
    PYFLAKES_CMD="${PRE_MERGE_PYFLAKES:-python3 -m pyflakes}"
    undef_base="$(git -C "$REPO" merge-base HEAD origin/main 2>/dev/null || true)"
    # -z: without it git C-quotes any path holding a non-ASCII byte, a quote or
    # a backslash ("caf\303\251.py"), the -f test below never matches the quoted
    # form, and the file drops out of the lane with nothing said.
    if [[ -n "$undef_base" ]]; then
        undef_scope="changed since merge base ${undef_base:0:9} with origin/main"
        mapfile -d '' -t UNDEF_FILES < <({ git -C "$REPO" diff -z --name-only --diff-filter=d "$undef_base" -- '*.py'
                                           git -C "$REPO" ls-files -z -o --exclude-standard -- '*.py'; } | sort -zu)
    else
        undef_scope="every tracked file (no merge base with origin/main)"
        mapfile -d '' -t UNDEF_FILES < <(git -C "$REPO" ls-files -z -- '*.py')
    fi
    UNDEF_EXISTING=()
    for f in "${UNDEF_FILES[@]}"; do [[ -f "$REPO/$f" ]] && UNDEF_EXISTING+=("$f"); done
    if ! (cd "$REPO" && $PYFLAKES_CMD --version >/dev/null 2>&1); then
        red   "FAIL  undefined names — pyflakes is not available ($PYFLAKES_CMD)"
        red   "      pip install -r scripts/requirements-gates.txt"
        failed=$((failed + 1)); failed_names+=("undefined names")
    elif [[ ${#UNDEF_EXISTING[@]} -eq 0 ]]; then
        green "OK    undefined names (no .py files changed, $((SECONDS - t0))s)"
    else
        # pyflakes prints its findings on stdout and everything else on stderr:
        # a file it cannot parse or decode, and its own traceback. A file it
        # could not read has had NO name checked, so stderr fails the lane;
        # filtered together with stdout for "undefined name", it read as a pass.
        undef_out="$(cd "$REPO" && $PYFLAKES_CMD "${UNDEF_EXISTING[@]}" 2>"$TMPD/undefined-names.err")"
        undef_unread="$(cat "$TMPD/undefined-names.err" 2>/dev/null)"
        undef_hits="$(printf '%s\n' "$undef_out" | grep -E ': undefined name |referenced before assignment' || true)"
        if [[ -z "$undef_hits" && -z "$undef_unread" ]]; then
            green "OK    undefined names (${#UNDEF_EXISTING[@]} .py files, $undef_scope, $((SECONDS - t0))s)"
        else
            red   "FAIL  undefined names (${#UNDEF_EXISTING[@]} .py files, $undef_scope)"
            [[ -n "$undef_hits" ]] && printf '%s\n' "$undef_hits" | head -40 | sed 's/^/      /'
            if [[ -n "$undef_unread" ]]; then
                red "      pyflakes could not read these, so none of their names were checked:"
                printf '%s\n' "$undef_unread" | head -20 | sed 's/^/      /'
            fi
            failed=$((failed + 1)); failed_names+=("undefined names")
        fi
    fi
    time_row "undefined names" "$_t_start" "-" lane
fi

elapsed=$((SECONDS - started))
# The banner may only claim what actually ran, and the denominator has to count
# the same things as the numerator: `failed` can now include tests/, which is
# not in GATES, so the total is gates + however many extra lanes ran.
suffix=""; total=${#GATES[@]}
[[ $TESTS_RAN -eq 1 ]] && { suffix=" + tests/"; total=$((total + 1)); }
[[ $UNDEF_RAN -eq 1 ]] && { suffix="$suffix + undefined names"; total=$((total + 1)); }
echo
record_failures
timing_report
echo
if [[ $failed -eq 0 && -n "$START_TREE" && -z "$AFFECTED_ONLY" ]] && _st="$(git -C "$REPO" status --porcelain 2>/dev/null)" \
   && [[ -z "$_st" && "$(git -C "$REPO" rev-parse 'HEAD^{tree}' 2>/dev/null)" == "$START_TREE" ]]; then
    # Clean at both ends and the same tree: the hash names exactly the code
    # that passed.
    date -u +%FT%TZ >"$RESULTS_DIR/$START_TREE.pass" 2>/dev/null || true
fi
if [[ $failed -eq 0 && -n "$STORE_MODE" ]]; then
    green "STORE-APP GATES PASSED — ${#GATES[@]} gates in ${elapsed}s"
    (( ${#retried_names[@]} )) && dim "  passed alone after failing in the pool: ${retried_names[*]}"
    exit 0
fi
if [[ $failed -eq 0 ]]; then
    green "PRE-MERGE GATES PASSED — ${#GATES[@]} gates${suffix} in ${elapsed}s"
    [[ -n "$AFFECTED_ONLY" ]] && dim "  affected-only: the gates that cover this change; the full suite runs at ship"
    [[ -n "$TESTS_SKIP_WHY" ]] && dim "  tests/ not run: $TESTS_SKIP_WHY"
    (( ${#retried_names[@]} )) && dim "  passed alone after failing in the pool: ${retried_names[*]}"
    echo
    dim "This is the container-free half only. scripts/smoke_test.sh still has to"
    dim "run at ship time for everything that needs a live image."
    exit 0
fi

if [[ -n "$STORE_MODE" ]]; then
    red "STORE-APP GATES FAILED — $failed of ${total} (${elapsed}s)"
    for n in "${failed_names[@]}"; do red "  - $n"; done
    exit 1
fi
red "PRE-MERGE GATES FAILED — $failed of ${total} (${elapsed}s)"
for n in "${failed_names[@]}"; do red "  - $n"; done
echo
red "Fix these BEFORE merging. Each one caught here costs seconds; the same"
red "failure found at ship time costs a merge, an image build and a smoke run."
exit 1
