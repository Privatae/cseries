#!/usr/bin/env bash
# store_app_gates.sh — the gates of the STORE apps, on their own lane.
#
# 2026-10-12: store apps are not kernel code, so their gates run separately.
# scripts/gates-store-apps.txt names every gate whose subject is a
# store app (one a person installs from the App Store; it reaches a Host on its
# own publish, not with the image) and the app it tests. The kernel lane
# (pre_merge_gates.sh, PR and ship, and smoke's container-free pass) never runs
# them; this does.
#
# USAGE
#   scripts/store_app_gates.sh --changed-from <ref>   the gates of the apps this
#        tree changed since its merge base with <ref>: a change under
#        system-apps/<id>/, to the gate file itself, to a path the gate's
#        registry line names, or to a scripts/ module the gate imports (directly
#        or through another; a tests/ gate's conftest.py too). Nothing changed
#        store-side: says so, exits 0.
#        (CI's `store-apps` job: --changed-from origin/main.)
#   scripts/store_app_gates.sh --app <id>              every gate of that app (the
#        store-lane publish runs this before it publishes the app)
#   scripts/store_app_gates.sh --all                   every store-app gate
#   --repo <path> runs against another checkout (default: this script's repo).
#
# The scripts/ gates run through pre_merge_gates.sh with PRE_MERGE_STORE_GATES
# set, so they get its parallel pool, per-gate limits (scripts/gate-timeouts.txt),
# hang_watch, the one retry alone of a load-sensitive gate, and its report. A
# tests/ entry runs under scripts/pytest_supervisor.py, as the kernel's tests/
# lane does, with that lane's backstop. Exit 0 = every selected gate passed.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODE="" VAL=""
while (( $# )); do
    case "$1" in
        --changed-from|--app) [[ $# -ge 2 && -n "$2" ]] || { echo "store_app_gates: $1 needs a value" >&2; exit 2; }
                              MODE="${1#--}"; VAL="$2"; shift 2 ;;
        --all) MODE=all; shift ;;
        --repo) [[ $# -ge 2 ]] || { echo "store_app_gates: --repo needs a path" >&2; exit 2; }
                REPO="$(cd "$2" && pwd)" || exit 2; shift 2 ;;
        -h|--help) sed -n '2,28p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) echo "store_app_gates: unknown argument $1 (see --help)" >&2; exit 2 ;;
    esac
done
[[ -n "$MODE" ]] || { echo "store_app_gates: say --changed-from <ref>, --app <id> or --all" >&2; exit 2; }

REGISTRY="$REPO/scripts/gates-store-apps.txt"
[[ -f "$REGISTRY" ]] || { echo "store_app_gates: no scripts/gates-store-apps.txt under $REPO" >&2; exit 2; }
if [[ "$MODE" == app && ! -d "$REPO/system-apps/$VAL" ]]; then
    echo "store_app_gates: there is no app $VAL (system-apps/$VAL)" >&2; exit 2
fi

CHANGED_FILE="$(mktemp)"
trap 'rm -f "$CHANGED_FILE"' EXIT
if [[ "$MODE" == changed-from ]]; then
    base="$(git -C "$REPO" merge-base HEAD "$VAL" 2>/dev/null || true)"
    if [[ -z "$base" ]]; then
        # Nothing is known to be unchanged, so every gate runs, as
        # pre_merge_gates.sh does without an origin/main.
        echo "store_app_gates: no merge base with $VAL: running every store-app gate" >&2
        MODE=all
    else
        { git -C "$REPO" diff -z --name-only --no-renames "$base"
          git -C "$REPO" ls-files -z -o --exclude-standard; } >"$CHANGED_FILE" 2>/dev/null
    fi
fi

# One selected entry per line, each with the apps that selected it.
mapfile -t SELECTED < <(python3 - "$REGISTRY" "$MODE" "$VAL" "$CHANGED_FILE" <<'PY'
import sys
reg, mode, val, changed_path = sys.argv[1:5]
raw = open(changed_path, "rb").read().decode("utf-8", "surrogateescape")
changed = [p for p in raw.split("\0") if p]
def under(path, prefix):
    p = prefix.rstrip("/")
    return path == p or path.startswith(p + "/")
import ast, os
repo = os.path.dirname(os.path.dirname(os.path.abspath(reg)))
def imports(rel):
    # The scripts/ modules one file imports, by either spelling: `import
    # gate_loopback` (scripts/ on sys.path) or `from scripts.gate_loopback ...` /
    # `from scripts import gate_loopback` (the repo root on it). A file that
    # cannot be read imports nothing here: a gate that is itself changed or
    # removed is selected by the rule above, and running it is what fails loudly.
    try:
        tree = ast.parse(open(os.path.join(repo, rel), encoding="utf-8").read())
    except (OSError, SyntaxError, ValueError):
        return set()
    mods = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            names = [a.name for a in n.names]
        elif isinstance(n, ast.ImportFrom) and n.module and not n.level:
            names = [n.module]
            if n.module == "scripts":
                names += ["scripts." + a.name for a in n.names]
        else:
            continue
        for name in names:
            parts = name.split(".")
            mods.add(parts[1] if parts[0] == "scripts" and len(parts) > 1 else parts[0])
    return {"scripts/" + m + ".py" for m in mods if os.path.isfile(os.path.join(repo, "scripts", m + ".py"))}
def helpers(own):
    # The scripts/ modules the gate imports, and the ones those import
    # (connector_gate_kit imports kernel_schema; the image-race gate imports the
    # Telegram gate, which imports gate_loopback): a change to one can break the
    # gate, so it selects the gate here. A kernel source change does not; the
    # kernel lane's broader rule (a gate whose text names a changed file) would
    # bring the store gates back onto every kernel pull request, which is what
    # this lane exists to stop.
    found, todo = set(), [own]
    # pytest loads a tests/ gate's conftest.py files without an import.
    d = os.path.dirname(own)
    while own.startswith("tests/") and d:
        if os.path.isfile(os.path.join(repo, d, "conftest.py")):
            found.add(d + "/conftest.py")
            todo.append(d + "/conftest.py")
        d = os.path.dirname(d)
    while todo:
        for m in imports(todo.pop()) - found:
            found.add(m)
            todo.append(m)
    return found
seen = {}
for line in open(reg, encoding="utf-8"):
    f = line.split("#", 1)[0].split()
    if len(f) < 2:
        continue
    gate, apps, extra = f[0], f[1].split(","), f[2:]
    own = gate if gate.startswith("tests/") else "scripts/" + gate
    if mode == "all":
        why = apps
    elif mode == "app":
        why = [a for a in apps if a == val]
    else:
        why = [a for a in apps if any(under(c, "system-apps/" + a) for c in changed)]
        if not why and any(c == own or any(under(c, x) for x in extra) for c in changed):
            why = apps
        if not why and helpers(own) & set(changed):
            why = apps
    if why:
        seen.setdefault(gate, [])
        seen[gate] += [a for a in why if a not in seen[gate]]
for gate, why in seen.items():
    print(gate + "\t" + ",".join(why))
PY
)

if (( ${#SELECTED[@]} == 0 )); then
    case "$MODE" in
        changed-from) echo "store-app gates: no store app changed since $VAL; nothing to run" ;;
        app) echo "store-app gates: $VAL has no gates in scripts/gates-store-apps.txt; nothing to run" ;;
        *) echo "store-app gates: scripts/gates-store-apps.txt lists no gate" ;;
    esac
    exit 0
fi

GATES=(); TESTS=()
echo "store-app gates ($MODE${VAL:+ $VAL}):"
for row in "${SELECTED[@]}"; do
    g="${row%%$'\t'*}"
    echo "  $g  (${row#*$'\t'})"
    if [[ "$g" == tests/* ]]; then TESTS+=("$g"); else GATES+=("$g"); fi
done
echo

rc=0
if (( ${#GATES[@]} )); then
    PRE_MERGE_STORE_GATES="${GATES[*]}" bash "$REPO/scripts/pre_merge_gates.sh" "$REPO" || rc=1
fi
if (( ${#TESTS[@]} )); then
    backstop="$(awk '$1 == "tests/" && $2 ~ /^[0-9]+$/ {print $2; exit}' "$REPO/scripts/gate-timeouts.txt" 2>/dev/null)"
    echo
    echo "store-app tests: ${TESTS[*]}"
    if (cd "$REPO" && CSERIES_REQUIRE_CHROMIUM=1 python3 scripts/pytest_supervisor.py \
            --backstop-s "${backstop:-180}" -- python3 -m pytest -q -p no:cacheprovider "${TESTS[@]}"); then
        echo "OK    store-app tests (${#TESTS[@]} files)"
    else
        echo "FAIL  store-app tests: ${TESTS[*]}"
        rc=1
    fi
fi
exit "$rc"
