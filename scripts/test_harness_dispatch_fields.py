#!/usr/bin/env python3
"""Gate: no harness dispatch path may silently DROP a configured run field.

`_run_on_pod` has always accepted an `effort`, and fan_out supplied one from
the saved per-harness overrides — but `_dispatch_to_pod_harness` had no
`effort` parameter at all, so the solo path (single-harness build,
merge_session) dropped it. The session manifest recorded `harness_effort`
either way, so the record claimed a setting applied on a path where it never
left the entity pod. Same saved config, two behaviours, no error.

A runtime test can't catch this class cheaply (it needs a live pod), and the
failure is silent by construction. So this is a STRUCTURAL gate on the dispatch
chain in server/routes/review.py:

  1. every dispatch function declares every configured field, and
  2. every call to one passes every field explicitly.

Rule (2) is what actually bites: adding a field to the signature and forgetting
one call site reproduces the exact bug, and a default of None makes it silent.
Passing `effort=None` deliberately is fine and visible — omitting it is not.

Known limits (a structural gate, not a proof): `**kwargs` forwarding is treated
as passing; a call through an alias or a wrapper is invisible to the name match;
and the scope is review.py only, so a dispatch added in another module is
ungated. The `call_sites > 0` backstop catches a wholesale restructure but not a
single aliased call.

Purely AST — no imports, no server, no DB. Run: python3 scripts/test_harness_dispatch_fields.py
"""
import ast
import os
import sys

REVIEW = os.path.join(
    os.environ.get("APP_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "server", "routes", "review.py",
)

# The per-run configuration fields the user sets in the Orchestrator's Coding
# section. Add a field here when the picker gains one and this gate will
# require it end to end.
FIELDS = ("model", "effort",
          # The build's target, which the Claude Code harness reads to keep
          # its benchmark rule off a repo change.
          "target")

# The dispatch chain, outermost first. Each hands off to the next.
# _dispatch_to_harness was dropped 2026-08-28: a pass-through whose only caller
# was merge_session_service, so retiring that path left it uncallable. Its
# doctrine moved onto _dispatch_to_pod_harness, now the outermost link. Do not
# re-add a name here without a definition in review.py — check 1 below fails on
# a missing one, which is exactly what would have caught a half-done removal.
DISPATCH_FNS = ("_dispatch_to_pod_harness", "_run_on_pod")

failures = []


def check(label, ok, detail=""):
    print(f"[dispatch-fields] {'ok  ' if ok else 'FAIL'} {label}"
          + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(label)


def main() -> int:
    try:
        with open(REVIEW, "r", encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=REVIEW)
    except Exception as e:
        check(f"parse {REVIEW}", False, repr(e))
        print("[dispatch-fields] FAIL (1 failing)")
        return 1

    defs: dict = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in DISPATCH_FNS:
                defs[node.name] = node

    # ── 1. every dispatch function declares every field ──────────────
    for fn in DISPATCH_FNS:
        node = defs.get(fn)
        if node is None:
            check(f"{fn} exists in review.py", False, "not found — was it renamed?")
            continue
        a = node.args
        params = {p.arg for p in a.posonlyargs + a.args + a.kwonlyargs}
        missing = [f for f in FIELDS if f not in params]
        check(f"{fn} declares {', '.join(FIELDS)}",
              not missing, f"missing: {missing}")

    # ── 2. every CALL to a dispatch function passes every field ──────
    # A dropped keyword is the bug: the parameter defaults to None and the run
    # silently uses the harness default while the saved config says otherwise.
    call_sites = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fnode = node.func
        called = fnode.id if isinstance(fnode, ast.Name) else getattr(fnode, "attr", None)
        if called not in DISPATCH_FNS:
            continue
        kwargs = {k.arg for k in node.keywords if k.arg}
        if any(k.arg is None for k in node.keywords):
            # **kwargs forwarding — can't prove statically; treat as passing.
            continue
        call_sites += 1
        line = getattr(node, "lineno", "?")
        missing = [f for f in FIELDS if f not in kwargs]
        check(f"call to {called} at review.py:{line} passes {', '.join(FIELDS)}",
              not missing, f"missing: {missing}")

    check("found dispatch call sites to check", call_sites > 0,
          "no calls matched — the chain may have been restructured")

    # ── credential write-back must be awaited ────────────────────────
    # _persist_rotated_creds became async so it could consult the disconnect
    # tombstone (a revoked harness must not have its cred file written back
    # after a run). An async def whose call site lost its `await` is a SILENT
    # no-op: no error, no write-back, and the OAuth rotation it exists to
    # persist is lost — the exact 2026-07-02 invalid_grant failure.
    for fn_name in ("_persist_rotated_creds",):
        node = None
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == fn_name:
                node = n
                break
        if node is None:
            check(f"{fn_name} exists", False, "not found — renamed?")
            continue
        if not isinstance(node, ast.AsyncFunctionDef):
            continue  # sync again: nothing to await, and that is a valid choice
        awaited, bare = 0, []
        for n in ast.walk(tree):
            if isinstance(n, ast.Await) and isinstance(n.value, ast.Call):
                f = n.value.func
                if (f.id if isinstance(f, ast.Name) else getattr(f, "attr", None)) == fn_name:
                    awaited += 1
        for n in ast.walk(tree):
            if not isinstance(n, ast.Expr) or not isinstance(n.value, ast.Call):
                continue
            f = n.value.func
            if (f.id if isinstance(f, ast.Name) else getattr(f, "attr", None)) == fn_name:
                bare.append(getattr(n, "lineno", "?"))
        check(f"{fn_name} is async and every call site awaits it",
              awaited > 0 and not bare,
              f"awaited={awaited}, un-awaited at lines {bare}")

    print(f"[dispatch-fields] {'PASS (0 failing)' if not failures else f'FAIL ({len(failures)} failing): {failures}'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
