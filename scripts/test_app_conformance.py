#!/usr/bin/env python3
"""App-facing /api conformance gate + contract generator (App SDK).

Two layers:
  * PURE (always runs; stdlib only): the extraction + matching + diff logic in
    api_contract.conformance_against, exercised against a hand-built contract. This is
    the false-positive-prone core, so it must pass everywhere (incl. the ops box).
  * LIVE (runs only where FastAPI is importable — the container / smoke): the
    build_api_contract introspection + the requires_permission scope tag.

Run:  python3 scripts/test_app_conformance.py         (from the cseries repo root)
Exit 0 = all ran checks passed.
"""
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Import api_contract.py DIRECTLY by path so the pure layer needs neither FastAPI nor
# the heavy server package __init__.
_spec = importlib.util.spec_from_file_location(
    "api_contract", os.path.join(ROOT, "server", "applications", "api_contract.py"))
api_contract = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(api_contract)

_checks = []


def check(name, cond, extra=""):
    _checks.append(cond)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + ("" if cond else f"   -> {extra}"))


class _Manifest:
    def __init__(self, permissions, network=None):
        self.permissions = permissions
        self.network = network or []
        self.entrypoint = "index.html"


def _app_dir(tmp, js):
    (tmp / "index.html").write_text('<script src="assets/app.js"></script>')
    (tmp / "app.js").write_text(js)
    return tmp


# A hand-built contract standing in for build_api_contract() output.
CONTRACT = {"version": 1, "count": 4, "routes": [
    {"method": "GET",  "path": "/api/widgets",       "scope": None},
    {"method": "POST", "path": "/api/widgets",       "scope": "write:projects"},
    {"method": "GET",  "path": "/api/widgets/{wid}", "scope": None},
    {"method": "GET",  "path": "/api/health",        "scope": None},
]}


def pure_layer():
    print("PURE layer (stdlib): extraction + matching + diff")
    # template->regex
    check("template_to_regex fills {param}",
          api_contract.template_to_regex("/api/x/{id}") == r"^/api/x/[^/]+$",
          api_contract.template_to_regex("/api/x/{id}"))

    with tempfile.TemporaryDirectory() as d:
        tmp = _app_dir(Path(d), "fetch('/api/widgets'); fetch('/api/widgets',{method:'POST'});")
        errs = api_contract.conformance_against(tmp, _Manifest(["write:projects"]), CONTRACT)
        check("clean app (real routes, scope declared) -> no violations", errs == [], str(errs))

    with tempfile.TemporaryDirectory() as d:
        tmp = _app_dir(Path(d), "fetch('/api/wgets');")  # typo
        errs = api_contract.conformance_against(tmp, _Manifest([]), CONTRACT)
        check("nonexistent route -> flagged",
              any("no server route" in e for e in errs), str(errs))

    with tempfile.TemporaryDirectory() as d:
        tmp = _app_dir(Path(d), "fetch('/api/widgets',{method:'POST'});")  # scoped, undeclared
        errs = api_contract.conformance_against(tmp, _Manifest([]), CONTRACT)
        check("scoped route without declared scope -> flagged",
              any("write:projects" in e for e in errs), str(errs))

    with tempfile.TemporaryDirectory() as d:
        tmp = _app_dir(Path(d), "fetch(`/api/widgets/${id}`);")  # dynamic tail
        errs = api_contract.conformance_against(tmp, _Manifest([]), CONTRACT)
        check("dynamic path tail (/api/widgets/${id}) NOT false-flagged", errs == [], str(errs))

    with tempfile.TemporaryDirectory() as d:
        # GET on a path that ALSO has a scoped POST must not demand the POST's scope.
        tmp = _app_dir(Path(d), "fetch('/api/widgets');")
        errs = api_contract.conformance_against(tmp, _Manifest([]), CONTRACT)
        check("GET on a path with a scoped sibling POST -> no false scope flag", errs == [], str(errs))

    with tempfile.TemporaryDirectory() as d:
        # A URL fragment must be stripped from the extracted path, not left to corrupt
        # it into a "no server route" false positive.
        tmp = _app_dir(Path(d), "fetch('/api/widgets#section');")
        errs = api_contract.conformance_against(tmp, _Manifest([]), CONTRACT)
        check("URL fragment (/api/widgets#frag) stripped, not false-flagged", errs == [], str(errs))

    with tempfile.TemporaryDirectory() as d:
        # WebSocket routes carry no .methods, so the contract never lists them; a ws()
        # call must therefore NOT be scanned (would be a guaranteed false "no server
        # route"). It stays an accepted false-negative until the contract carries ws.
        tmp = _app_dir(Path(d), "new WebSocket('/api/definitely-not-a-route');")
        errs = api_contract.conformance_against(tmp, _Manifest([]), CONTRACT)
        check("WebSocket call NOT extracted (no false 'no server route')", errs == [], str(errs))


def live_layer():
    try:
        from fastapi import FastAPI, Depends  # noqa: F401
    except Exception:
        print("LIVE layer: SKIPPED (FastAPI not importable here — covered at ship/smoke)")
        return
    try:
        sys.path.insert(0, ROOT)
        from server.applications.middleware import requires_permission
        from server.applications import api_contract as live_api_contract
    except Exception as e:
        print(f"LIVE layer: SKIPPED (server package not importable: {e})")
        return
    print("LIVE layer (FastAPI): introspection + requires_permission scope tag")
    from fastapi import FastAPI, Depends

    app = FastAPI()

    @app.get("/api/widgets")
    async def _w():
        return {}

    @app.post("/api/widgets", dependencies=[Depends(requires_permission("write:projects"))])
    async def _wc():
        return {}

    contract = live_api_contract.build_api_contract(app)
    by = {(r["method"], r["path"]): r["scope"] for r in contract["routes"]}
    check("contract lists GET /api/widgets", ("GET", "/api/widgets") in by, str(by))
    check("requires_permission scope recovered by introspection",
          by.get(("POST", "/api/widgets")) == "write:projects", str(by))
    check("unscoped route has scope None", by.get(("GET", "/api/widgets")) is None, str(by))
    check("only /api routes included", all(r["path"].startswith("/api/") for r in contract["routes"]),
          str([r["path"] for r in contract["routes"]]))

    # The kernel declares nearly every route on an APIRouter that server.main
    # includes, and from fastapi 0.137.0 app.routes holds the included router
    # rather than its routes. A contract read from app.routes then lists none
    # of them (measured on 0.141.1: 0 routes). So the routes here come in
    # through include_router, with the prefix and the scope on the include.
    from fastapi import APIRouter, WebSocket
    from starlette.routing import WebSocketRoute
    from server.route_table import declared_route, effective_routes

    gadgets = APIRouter()

    @gadgets.get("/{gid}")
    async def _g(gid: str):
        return {}

    @gadgets.get("/hidden", include_in_schema=False)
    async def _gh():
        return {}

    @gadgets.websocket("/live")
    async def _gl(ws: WebSocket):
        await ws.close()

    app.include_router(gadgets, prefix="/api/gadgets",
                       dependencies=[Depends(requires_permission("read:projects"))])
    contract = live_api_contract.build_api_contract(app)
    by = {(r["method"], r["path"]): r["scope"] for r in contract["routes"]}
    check("a route reached through include_router is listed at its prefixed path",
          ("GET", "/api/gadgets/{gid}") in by, str(sorted(by)))
    check("the scope set on the include is recovered for that route",
          by.get(("GET", "/api/gadgets/{gid}")) == "read:projects", str(by))
    check("an included include_in_schema=False route stays out",
          ("GET", "/api/gadgets/hidden") not in by, str(sorted(by)))
    # generate_capabilities.py lists the /api WebSocket routes the same way.
    ws = [r.path for r in effective_routes(app) if isinstance(declared_route(r), WebSocketRoute)]
    check("an included WebSocket route is seen at its prefixed path",
          ws == ["/api/gadgets/live"], str(ws))


def main():
    pure_layer()
    live_layer()
    passed = sum(1 for c in _checks if c)
    print(f"\n{passed}/{len(_checks)} checks passed")
    return 0 if all(_checks) else 1


if __name__ == "__main__":
    sys.exit(main())
