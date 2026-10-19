#!/usr/bin/env python3
"""Invariant: an app's DATA-DECLARED API calls are covered by its manifest scopes.

THE HOLE THIS FILLS. `server/applications/api_contract.py::conformance_against`
already diffs an app's calls against the live route table at build time, and it
is a good gate — but it can only see calls it can PARSE, and its extractor
(`_extract_api_calls`) regexes literal `fetch('/api/...')` / `new EventSource(
'/api/...')` call sites. The Getting Started tour does not write its checks that
way: every stop declares its live check as DATA (`endpoint: '/api/...'` in the
step table at system-apps/getting-started/steps.js) and one generic runner does
the fetching. Not one of those nine endpoints is visible to the existing gate.

WHAT THAT COST, twice, in one app, both found by hand:
  - The Compute stop called GET /api/compute/summary (read:inference_log) while
    the manifest still declared read:topology, for the OLD /api/network/overview
    endpoint. A brand-new user's tour showed "not yet" on that row FOREVER, with
    fix text telling them to give it a moment.
  - The Circles stop calls GET /api/circles/conversations. That router was
    ungated (14 routes, zero requires_permission) until 2026-08-25, so the check
    passed for the wrong reason. The moment the router was gated the same
    permanent 403 would have shipped — a security fix breaking the first-run
    experience, which is the worst possible way to find this out.

Both are the same defect: a scope declared in a manifest and an endpoint called
by the app drifted apart, and nothing compared them. A first-run tour is where a
silent permanent 403 does the most damage, because the user has no prior
experience telling them the row is lying.

SCOPE OF THE CHECK. Deliberately narrow, so it only ever fails on a real bug:
only the data-declared shape, and only where the route table gives an
UNAMBIGUOUS answer (exactly one route template matches path+method). Anything
ambiguous is printed as SKIP with its reason — never counted as a pass, never as
a failure. The fetch-literal shape is already covered by conformance_against at
build time and is not re-implemented here.

The route table is NOT re-derived: it comes from build_api_contract(), the same
introspection the app-facing contract and the capabilities doc are built from,
so a route's scope is read from the dependency FastAPI actually resolved.

RUN:  python3 scripts/test_app_manifest_scopes.py   (from the repo root, or with
      APP_ROOT set). Imports server.main the way scripts/generate_capabilities.py
      does — no database required.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

APP_ROOT = Path(os.environ.get("APP_ROOT", Path(__file__).resolve().parent.parent))
# Both entries, in this order — same as scripts/generate_capabilities.py. The
# brain modules are imported BARE (`ceigas_domain_controller`, `mastercode.*`),
# not as `brain.*`, so server.main will not import without brain/ on the path.
sys.path.insert(0, str(APP_ROOT))
sys.path.insert(0, str(APP_ROOT / "brain"))

# `endpoint: '/api/...'`, plus the optional `method: 'POST'` that governs it.
# Backticks are accepted alongside quotes: a template literal is the natural
# thing to reach for when someone interpolates a step's endpoint, and a scanner
# that silently stops seeing its input is the failure this gate exists to catch.
_ENDPOINT_RE = re.compile(r"""endpoint\s*:\s*['"`](/api/[^'"`]*)['"`]""")
_METHOD_RE = re.compile(r"""method\s*:\s*['"`]([A-Za-z]+)['"`]""")

# Built output, skipped: a React app's bundle already went through the
# fetch-literal lane at build time, and scanning minified source here only
# invents matches out of mangled identifiers.
_SKIP_DIRS = {"assets", "dist", "build", "node_modules"}

# Apps KNOWN to declare their API calls as data. Each must still contribute at
# least one endpoint, or this gate failed to see its input and reported OK on an
# empty scan — which is the precise failure it was built to catch, wearing the
# gate's own uniform. Adding a stop is free; removing the last one is loud, and
# so is renaming the key or moving the file somewhere the scan does not reach.
# Keep this list SMALL: an app belongs here once it actually uses the shape.
_MUST_CONTRIBUTE = {"getting-started"}


def _declared_endpoints(js: str) -> list[tuple[str, str]]:
    """(path, method) for every data-declared check in a step table.

    The governing method is the LAST `method:` between the previous endpoint and
    this one; absent means GET, which is what the tour's runner does (a plain
    fetch with no options unless the step declares otherwise).

    Why not brace-match the enclosing object: a step's `action.request` is a
    NESTED object carrying its own method, and the payloads in these tables
    contain inline CSS ("body{margin:0}"), so naive brace counting mis-scopes in
    both directions. Measured on system-apps/getting-started/steps.js, brace
    matching read the check's GET /api/artifacts?limit=50 as a POST, because the
    nested create-action's method sits between it and the first closing brace.
    Slicing on the previous endpoint is exact for this shape — `method` always
    precedes the `endpoint` it applies to.

    KNOWN FAILURE DIRECTION, stated because the earlier wording here overclaimed
    it as safe: if a future object ever writes `endpoint:` BEFORE its `method:`,
    the method attaches to the next endpoint and this one is judged as GET. That
    is loud (a wrong-verb match usually finds no route, or finds one whose scope
    is undeclared) — but it is not loud in every case. If a GET route also lives
    at that path and needs a scope the app happens to declare, the gate judges
    the wrong route and stays green. So: keep `method` before `endpoint`.
    """
    out: list[tuple[str, str]] = []
    prev_end = 0
    for m in _ENDPOINT_RE.finditer(js):
        between = js[prev_end:m.start()]
        methods = _METHOD_RE.findall(between)
        out.append((m.group(1), (methods[-1] if methods else "GET").upper()))
        prev_end = m.end()
    return out


def main() -> int:
    from server.main import app as fastapi_app  # noqa: E402
    from server.applications.api_contract import build_api_contract, _index  # noqa: E402
    from server.applications import manifest as manifest_mod  # noqa: E402

    idx = _index(build_api_contract(fastapi_app))
    if not idx:
        print("FAIL  no /api routes introspected — the gate cannot run", file=sys.stderr)
        return 1

    apps_root = APP_ROOT / "system-apps"
    if not apps_root.is_dir():
        print(f"FAIL  {apps_root} is not on disk — the gate scanned nothing. In the "
              f"image lane the source apps must be mounted (smoke_test.sh does this); "
              f"a run that finds no input must not report OK.", file=sys.stderr)
        return 1

    failures: list[str] = []
    skips: list[str] = []
    checked = 0
    contributed: set[str] = set()

    for app_dir in sorted(apps_root.iterdir()):
        if not app_dir.is_dir() or not (app_dir / "app.yaml").exists():
            continue
        pairs: list[tuple[str, str, str]] = []      # (source file, path, method)
        for js in sorted(app_dir.rglob("*.js")):
            # Skip BUILT output. A React app's bundle is minified source that
            # already went through the fetch-literal lane, and scanning it here
            # only invents matches out of mangled identifiers.
            if any(part in _SKIP_DIRS for part in js.relative_to(app_dir).parts[:-1]):
                continue
            text = js.read_text(encoding="utf-8", errors="replace")
            for path, verb in _declared_endpoints(text):
                pairs.append((str(js.relative_to(apps_root)), path, verb))
        if not pairs:
            continue
        contributed.add(app_dir.name)

        mf = manifest_mod.load(app_dir / "app.yaml")
        declared = {str(s) for s in (mf.permissions or [])}

        for rel, raw_path, verb in pairs:
            checked += 1
            path = raw_path.split("?", 1)[0].split("#", 1)[0]
            hits = [r for r in idx if r["method"] == verb and r["regex"].match(path)]
            if not hits:
                failures.append(
                    f"{rel}: declares {verb} {raw_path!r} but NO route matches it — "
                    f"the check can never pass (a typo, or the endpoint moved)")
                continue
            if len({r["path"] for r in hits}) > 1:
                skips.append(
                    f"{rel}: {verb} {path} matches {len(hits)} route templates "
                    f"({', '.join(sorted({r['path'] for r in hits}))}) — ambiguous, not judged")
                continue
            scope = hits[0]["scope"]
            # A route that takes any one of several scopes (requires_any_permission)
            # is covered by any of them.
            anyof = hits[0].get("scopes_any") or ([scope] if scope else [])
            if scope is None:
                print(f"ok    {rel}: {verb} {path} — route is unscoped, nothing to declare")
            elif any(sc in declared for sc in anyof):
                print(f"ok    {rel}: {verb} {path} — needs {' or '.join(anyof)}, declared")
            else:
                failures.append(
                    f"{rel}: {verb} {raw_path!r} (route {hits[0]['path']}) requires scope "
                    f"{scope!r}, but {app_dir.name}/app.yaml permissions[] do not declare "
                    f"it — this 403s at runtime and the check reads 'not yet' forever")

    for s in skips:
        print(f"SKIP  {s}")

    # The floor, checked BEFORE the verdict: a scan that found nothing is not a
    # pass. Without this the gate reports OK the day steps.js renames its key,
    # moves to a path the scan misses, or is simply not mounted — silently, and
    # forever, on exactly the input it exists to read.
    silent = sorted(_MUST_CONTRIBUTE - contributed)
    if silent:
        print(f"\nFAIL  app manifest scopes — scanned {sorted(contributed)} but found NO "
              f"data-declared endpoint in: {silent}. These apps declare their API calls "
              f"as data, so zero means the scanner stopped seeing its input (a renamed "
              f"key, a moved file, an unmounted source tree) — not that there is nothing "
              f"to check. Fix the scan; do NOT drop the app from _MUST_CONTRIBUTE unless "
              f"it genuinely stopped using the shape.", file=sys.stderr)
        return 1

    if failures:
        print(f"\nFAIL  app manifest scopes — {len(failures)} of {checked} data-declared "
              f"endpoints are not covered:", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1
    print(f"\nOK    app manifest scopes — {checked} data-declared endpoints across "
          f"{sorted(contributed)}, every scoped one declared "
          f"({len(skips)} ambiguous, not judged)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
