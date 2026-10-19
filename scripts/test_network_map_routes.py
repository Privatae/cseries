#!/usr/bin/env python3
"""The host-authored map's FLOOR and ROUTES, over real HTTP.

The browser suite drives the map through a local harness that serves the two
paths as fixtures. That proves the app consumes them; it proves nothing about
the real FastAPI routes, which were added and never called. This mounts the
real `appearance.router` on an ASGI app and issues real requests, and exercises
the floor directly (it is a pure function over the rendered document,
desktop_contract.py:125).

What it pins:
  the floor — a typed node list is refused as an invented topology; a document
              that reads the bridge passes; an external stylesheet is refused
              (the frame has no network); a missing mode meta is refused; and
              tokens are NOT required, so nobody "tightens" that into the
              desktop's rule by reflex.
  the routes — /api/appearance/network and /network/meta are registered, and
              with no brain pool they ANSWER 503 rather than crashing. That is
              the path a pod hits while the brain is still coming up.

Run:  python3 scripts/test_network_map_routes.py
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
import types

_ROOT = os.environ.get("APP_ROOT") or os.path.join(os.path.dirname(__file__), "..")
sys.path[:0] = [_ROOT, os.path.join(_ROOT, "brain"), os.path.join(_ROOT, "system-apps")]

failures: list[str] = []
notes: list[str] = []


def check(cond, msg):
    if cond:
        notes.append("  PASS  " + msg)
    else:
        failures.append(msg)


HONEST_MAP = (
    '<!DOCTYPE html><html><head><meta name="cseries-network" content="map"></head>'
    '<body><div data-cseries-nodes></div>'
    '<script>CSeriesNetwork.on(function(s){});</script></body></html>'
)
INVENTED_MAP = (
    '<!DOCTYPE html><html><head><meta name="cseries-network" content="map"></head>'
    '<body><div id="m"></div><script>var nodes=['
    '{id:"a",label:"office-nas"},{id:"b",label:"studio-mac"},{id:"c",label:"rack-01"}];'
    '</script></body></html>'
)
EXTERNAL_MAP = (
    '<!DOCTYPE html><html><head><meta name="cseries-network" content="map">'
    '<link rel="stylesheet" href="https://cdn.example.com/x.css"></head>'
    '<body><div data-cseries-nodes></div></body></html>'
)


async def main() -> int:
    # server.main mounts container paths at IMPORT; appearance imports it.
    _stub_main = types.ModuleType("server.main")
    _stub_main.app = None
    sys.modules.setdefault("server.main", _stub_main)

    from server.applications.desktop_contract import check_network_document

    # ── the floor, directly ─────────────────────────────────────────────
    good = check_network_document(HONEST_MAP)
    check(good["ok"], f"a map that reads the bridge passes the floor: {good['errors']}")

    bad = check_network_document(INVENTED_MAP)
    check(not bad["ok"], "a map with a typed node list is refused")
    check(any("node-shaped objects" in e for e in bad["errors"]),
          f"the refusal names the invented topology: {bad['errors']}")
    check(any("nothing draws the real network" in e for e in bad["errors"]),
          f"the refusal says the map never read the bridge: {bad['errors']}")

    ext = check_network_document(EXTERNAL_MAP)
    check(not ext["ok"] and any("external reference" in e for e in ext["errors"]),
          f"an external stylesheet is refused (the frame has no network): {ext['errors']}")

    nomode = check_network_document('<html><body><div data-cseries-nodes></div></body></html>')
    check(not nomode["ok"] and any("no mode declared" in e for e in nomode["errors"]),
          f"a document with no cseries-network meta is refused: {nomode['errors']}")

    check(good["ok"] and not good["tokens"],
          "a map needs no design tokens — requiring them would change nothing")

    # ── the routes ──────────────────────────────────────────────────────
    from fastapi import FastAPI
    import httpx
    import server.routes.appearance as appearance

    # brain_service.get_pool() returns the pod's pool or None
    # (brain_service.py:181-183). None is the pre-boot state, and these routes
    # must answer it rather than raise.
    class NoPool:
        _entity_id = 42

        @staticmethod
        def get_pool():
            return None
    appearance.brain_service = NoPool()

    app = FastAPI()
    app.include_router(appearance.router)
    # Read the ROUTER's own list, not app.routes: FastAPI 0.141 wraps an
    # included router in an _IncludedRouter rather than flattening its routes
    # onto the app, so app.routes reports only the four defaults. Asserting
    # there gave a false negative while the routes plainly answered — the live
    # requests below are the real proof either way.
    paths = {getattr(r, "path", None) for r in appearance.router.routes}
    for p in ("/api/appearance/network", "/api/appearance/network/meta"):
        check(p in paths, f"{p} is registered on the router")

    for dep in list(getattr(appearance, "_WRITE", []) or []):
        try:
            app.dependency_overrides[dep.dependency] = lambda: None
        except AttributeError:
            pass

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Both reads take read:topology since 2026-10-01 — the map is the Host's
        # drawing of the user's machines, and it was readable by any app. With
        # no token the guard answers first (scripts/test_api_route_guards.py
        # covers the scope itself); past it, the no-pool answer is the subject.
        r = await client.get("/api/appearance/network/meta")
        check(r.status_code == 401, f"meta needs a token (got {r.status_code})")
        for r_ in appearance.router.routes:
            if (getattr(r_, "path", None) in ("/api/appearance/network", "/api/appearance/network/meta")
                    and "GET" in (getattr(r_, "methods", None) or ())):
                for d in getattr(r_, "dependencies", []) or []:
                    if getattr(d, "dependency", None) is not None:
                        app.dependency_overrides[d.dependency] = lambda: None
        r = await client.get("/api/appearance/network/meta")
        check(r.status_code == 503,
              f"meta answers 503 with no pool, never a crash (got {r.status_code})")
        r = await client.get("/api/appearance/network")
        check(r.status_code == 503,
              f"the document route answers 503 with no pool (got {r.status_code})")
        # Accepting 401 here would prove nothing: the permission dependency
        # fires first, so the JSON-parse branch is never reached and the check
        # passes without exercising what its text claims. appearance.py declares
        # its permissions INLINE per route (dependencies=[Depends(...)]) rather
        # than through a module-level _WRITE list, so the override has to be
        # taken off the route itself.
        for r_ in appearance.router.routes:
            if getattr(r_, "path", None) == "/api/appearance/network":
                for d in getattr(r_, "dependencies", []) or []:
                    if getattr(d, "dependency", None) is not None:
                        app.dependency_overrides[d.dependency] = lambda: None
        r = await client.put("/api/appearance/network", content="not json")
        check(r.status_code in (400, 503),
              f"a non-JSON body reaches the handler and is refused there, not parsed "
              f"(got {r.status_code}; a 401 would mean auth answered and this proved nothing)")

    # ── the two guarantees the floor STATES, which must be enforced and not
    #    merely asserted in an error string ────────────────────────────────────
    # Read the FUNCTION BODY out of the source file by AST, not inspect: an
    # earlier cut of this check used inspect.getsource and passed with the fix
    # deleted, because linecache had the pre-edit file. A gate that cannot go
    # red is not a gate, so this one is derived from the parsed tree and was
    # verified to fail with the call removed.
    import ast
    _app_src = open(os.path.join(_ROOT, "server/routes/appearance.py"), encoding="utf-8").read()
    _tree = ast.parse(_app_src)
    _serve = next((n for n in ast.walk(_tree)
                   if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
                   and n.name == "serve_network_html"), None)
    check(_serve is not None, "serve_network_html exists in server/routes/appearance.py")
    _calls = {n.func.id for n in ast.walk(_serve) if isinstance(n, ast.Call)
              and isinstance(n.func, ast.Name)} if _serve else set()
    # The floor ran only in set_network_artifact — once, when the pointer was
    # set. But the pointer is deliberately live: /network/meta exists so the
    # host can iterate the artifact and watch the map update. So the document
    # SERVED is not the document that was checked. Point at a compliant
    # artifact, rewrite it, and every guarantee the floor makes is bypassed by
    # the ordinary editing flow.
    check("check_network_document" in _calls,
          "the floor is re-checked on every render, not only when the pointer was set "
          "(a one-time gate on a live pointer is not a gate)")

    app_js = open(os.path.join(_ROOT, "system-apps/network/app.js"), encoding="utf-8").read()
    # sandbox="allow-scripts" gives the frame an opaque origin but does NOT take
    # away the network: fetch(), new Image().src and a form POST all still
    # egress. The floor's own message says "the map frame has no network" — only
    # a CSP can make that sentence true, and for a srcdoc frame the only CSP
    # that applies is a <meta http-equiv> injected into the document itself
    # (response headers do not reach srcdoc; the iframe csp= attribute is a
    # Chrome-only proposal).
    # The policy now lives in ONE place for all four host-authored surfaces
    # (dashboard/base/js/sandbox-csp.js). These are STRUCTURE checks — that the
    # map mounts through it and that it declares the directives. They are still
    # source greps, and that is still not enforcement: the behavioural proof is
    # scripts/test_sandbox_csp.py and scripts/e2e_hosted_map_egress.py, which
    # run the lanes in a real browser. Keep both — this one fails fast in the
    # container-free lane, those know what a browser actually does.
    check("CSeriesSandbox" in app_js,
          "the map mounts through the shared sandbox policy, not its own copy")
    shared = open(os.path.join(_ROOT, "dashboard/base/js/sandbox-csp.js"), encoding="utf-8").read()
    check("connect-src 'none'" in shared and "default-src 'none'" in shared,
          "the shared policy denies by default and closes connect-src")
    check("/js/sandbox-csp.js" in open(
              os.path.join(_ROOT, "system-apps/network/index.html"), encoding="utf-8").read(),
          "the Network app loads the shared policy before its own script")
    check("cseries:network:leaving" in app_js,
          "the frame reports its own departure, so a self-navigation is visible "
          "rather than silent (detection — the request has already left)")
    # The order inside the injected prefix: policy first, then the bridge, then
    # the document. Asserted on the concatenation itself rather than on a
    # variable name, so a rename does not quietly stop checking anything — the
    # last version of this looked for "HOSTED_CSP +" and went green on a
    # renamed symbol.
    concat = re.search(r"doc\.slice\(0, at\)\s*\+([^;]*?)\+\s*doc\.slice\(at\)", app_js, re.S)
    check(bool(concat), "the injected prefix is built by concatenation before the document")
    if concat:
        seq = concat.group(1)
        csp_at, bridge_at = seq.find("csp"), seq.find("HOSTED_BRIDGE")
        check(csp_at != -1 and bridge_at != -1 and csp_at < bridge_at,
              "the CSP is injected AHEAD of the bridge, so it is in force before any of "
              f"the document's own markup is parsed (order: {seq.strip()!r})")

    for line in notes:
        print(line)
    if failures:
        print(f"\nFAIL — {len(failures)} problem(s):")
        for f in failures:
            print("  · " + f)
        return 1
    print(f"\nok — {len(notes)} checks passed (floor + real routes)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
