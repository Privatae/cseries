"""`_run_on_pod` is the one place rotated credentials are written back — keep it so.

A vendor CLI may rotate its own token during a run (Claude OAuth refresh
rotates the refresh token). The pod is ephemeral, so the sidecar hands the
new file back on the /run body as `rotated_creds` and the kernel writes it to
the entity's cred file with `_persist_rotated_creds` — or the stored grant
dies with the pod and the next wake fails with invalid_grant (hit 2026-07-02).

That write-back lives at the END of `_run_on_pod`, and `_run_on_pod` is the
single function both dispatch shapes go through — solo
(`_dispatch_to_pod_harness`) and fan-out (`_harness_group` → `_variant` →
`_one_run`, all nested in `_run_fan_out_background`; `_variant` is the
per-run step that holds one of the pod's run slots). That is what makes "runs after
EVERY harness run" true, and it is a property that is easy to break by moving
the call up into one caller: on 2026-09-22 a review of the public trust page
read line 2122 as belonging to the solo caller and reported fan-out as
dropping rotations; the AST says otherwise (the line is inside `_run_on_pod`),
and this test pins that so the next reader does not have to re-derive it.

Source-level via `ast` (no pod, no aiohttp, no stubs). Four assertions:
`_run_on_pod`'s own body calls `_persist_rotated_creds`; both dispatch shapes
call `_run_on_pod`; the fan-out driver `_harness_group` actually reaches
`_one_run` through `_variant` (so the guard cannot be satisfied by dead code); and each of those
names is defined exactly once in the module (so a name-keyed lookup cannot
silently pick a shadowing helper).

Run with APP_ROOT pointing at a checkout to test that tree; defaults to this
repo.
"""
import ast
import os
from collections import Counter
from pathlib import Path

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
REVIEW = ROOT / "server" / "routes" / "review.py"

FunctionNode = (ast.FunctionDef, ast.AsyncFunctionDef)

CHOKE = "_run_on_pod"
PERSIST = "_persist_rotated_creds"
SOLO = "_dispatch_to_pod_harness"
FANOUT_RUN = "_one_run"
FANOUT_DRIVER = "_harness_group"
FANOUT_STEP = "_variant"


def _direct_calls(fn: ast.AST) -> set[str]:
    """Names called in `fn`'s own body, NOT counting anything inside a nested
    function or lambda — `_redispatch` is nested in `_run_on_pod`, and a call
    made there must not be credited to (or debited from) its parent."""
    names: set[str] = set()
    stack = list(fn.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (*FunctionNode, ast.Lambda)):
            continue
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name):
                names.add(f.id)
            elif isinstance(f, ast.Attribute):
                names.add(f.attr)
        stack.extend(ast.iter_child_nodes(node))
    return names


def test_run_on_pod_persists_rotated_creds_and_both_dispatch_shapes_use_it():
    tree = ast.parse(REVIEW.read_text(), filename=str(REVIEW))
    all_fns = [n for n in ast.walk(tree) if isinstance(n, FunctionNode)]

    # Each name this test keys on must be defined exactly once, or the dict
    # below would pick one definition by walk order with no warning.
    counts = Counter(n.name for n in all_fns)
    for name in (CHOKE, SOLO, FANOUT_RUN, FANOUT_DRIVER, FANOUT_STEP):
        assert counts[name] == 1, (
            f"{name} is defined {counts[name]} times in review.py — this test "
            "keys on it by name and needs exactly one definition"
        )
    fns = {n.name: n for n in all_fns}

    assert PERSIST in _direct_calls(fns[CHOKE]), (
        f"{CHOKE} no longer writes rotated credentials back itself; if the call "
        "moved into a caller, every OTHER caller now drops a token the vendor "
        "rotated during the run"
    )

    callers = sorted(name for name, fn in fns.items()
                     if name != CHOKE and CHOKE in _direct_calls(fn))
    assert {SOLO, FANOUT_RUN} <= set(callers), (
        f"expected both the solo and the fan-out dispatch to go through {CHOKE}; "
        f"callers found: {callers}"
    )

    # The fan-out guard is only worth something if the fan-out driver still
    # runs variants through _one_run — dead code satisfies the line above.
    assert FANOUT_STEP in _direct_calls(fns[FANOUT_DRIVER]), (
        f"{FANOUT_DRIVER} no longer calls {FANOUT_STEP}; if fan-out got a new "
        "per-variant runner, it must go through _run_on_pod too, and this test "
        "must learn its name"
    )
    assert FANOUT_RUN in _direct_calls(fns[FANOUT_STEP]), (
        f"{FANOUT_STEP} no longer calls {FANOUT_RUN}; if fan-out got a new "
        "per-variant runner, it must go through _run_on_pod too, and this test "
        "must learn its name"
    )
