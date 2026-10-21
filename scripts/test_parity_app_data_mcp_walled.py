#!/usr/bin/env python3
"""The kept app data and the MCP sign-in routes are classified for the Host.

Kept app data (server/routes/applications.py, /runtime/app-data): the Host may
READ the list, so it can tell the person what data removed apps left behind on
this machine; deleting it is permanent, so only the person does it, in the App
Store. MCP sign-in (server/routes/mcp_oauth.py): starting a sign-in, signing out
and the authorization server's callback decide which account the Host acts as
on that server, so all three are the person's, in the Network app.

Checks, against the real modules:
  1. every /runtime/app-data route applications.py registers is either the GET
     list, exposed to kernel_op and not walled, or the DELETE, walled by path
     (read from the router's own decorators, so a new method there fails here);
  2. kernel_op refuses a concrete DELETE .../app-data/<id> with the app-data
     reason, matched by its template, and lets the GET list through;
  3. every route mcp_oauth.py registers is in kernel_ops._WALLED_PATHS, and
     kernel_op refuses each with the sign-in reason, not the credential text;
  4. generate_capabilities declares both areas [] (reviewed), so the routes
     derive covered or walled rather than unreviewed or unreachable.
"""
import ast
import asyncio
import os
import re
import sys

ROOT = os.environ.get("APP_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (os.path.join(ROOT, "scripts"), os.path.join(ROOT, "brain"), ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)

FAILS: list[str] = []

APP_DATA = "/api/applications/runtime/app-data"
APP_DATA_ONE = "/api/applications/runtime/app-data/{app_id}"


def check(name: str, ok: bool, detail=None) -> None:
    print(("ok   " if ok else "FAIL ") + name)
    if not ok:
        FAILS.append(name)
        if detail is not None:
            print(f"     {detail!r}"[:600])


def router_routes(rel: str) -> list[tuple[str, str]]:
    """(METHOD, full path) for every @router.<method>("...") in one route module."""
    src = open(os.path.join(ROOT, *rel.split("/")), encoding="utf-8").read()
    tree = ast.parse(src)
    prefix = ""
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "APIRouter":
            for kw in n.keywords:
                if kw.arg == "prefix":
                    prefix = ast.literal_eval(kw.value)
    out = []
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for d in n.decorator_list:
                if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) \
                        and getattr(d.func.value, "id", None) == "router" and d.args:
                    out.append((d.func.attr.upper(), prefix + ast.literal_eval(d.args[0])))
    return out


def main() -> int:
    from mastercode.runtime import kernel_ops as ko

    # 1 and 2: kept app data
    data_routes = [r for r in router_routes("server/routes/applications.py")
                   if r[1] == APP_DATA or r[1].startswith(APP_DATA + "/")]
    check("1. applications.py registers GET app-data and DELETE app-data/{app_id}",
          sorted(data_routes) == [("DELETE", APP_DATA_ONE), ("GET", APP_DATA)], data_routes)
    check("1. the GET list is under an exposed kernel_op prefix",
          any(APP_DATA == p or APP_DATA.startswith(p + "/") for p in ko._EXPOSED_PREFIXES))
    check("1. the GET list is not walled", ko._walled_path_for(APP_DATA) is None)
    check("1. the DELETE is walled by path", APP_DATA_ONE in ko._WALLED_PATHS)
    reason = ko._WALL_REASONS.get(APP_DATA_ONE, "")
    check("2. the DELETE's reason names the App Store and the read the Host keeps",
          "App Store" in reason and f"GET {APP_DATA}" in reason and "credential" not in reason,
          reason)
    check("2. a concrete DELETE .../app-data/<id> is walled by its template",
          ko._walled_path_for(APP_DATA + "/photos") == APP_DATA_ONE)
    # The contract is the baked capabilities.json inside the image; the rows here
    # stand in for it so exposed_route answers on the host. What is under test is
    # the exposure and the wall, not the contract.
    saved = ko._contract_cache
    ko._contract_cache = {
        ("GET", APP_DATA): {"method": "GET", "path": APP_DATA, "scope": "execute:install"},
        ("DELETE", APP_DATA_ONE): {"method": "DELETE", "path": APP_DATA_ONE,
                                   "scope": "execute:install"},
    }
    try:
        check("2. kernel_op exposes GET app-data", ko.exposed_route("GET", APP_DATA) is not None)
        check("2. kernel_op does not expose the DELETE template",
              ko.exposed_route("DELETE", APP_DATA_ONE) is None)
        res = asyncio.run(ko.run_kernel_op({"method": "DELETE", "path": APP_DATA + "/photos"}))
        check("2. kernel_op DELETE .../app-data/photos is denied with the app-data reason",
              res.get("status") == "denied" and res.get("result") == reason, res)
    finally:
        ko._contract_cache = saved

    # 3: MCP sign-in
    mcp = router_routes("server/routes/mcp_oauth.py")
    check("3. mcp_oauth.py registers its three routes under /api/mcp/oauth",
          len(mcp) == 3 and all(p.startswith("/api/mcp/oauth/") for _m, p in mcp), mcp)
    for method, path in mcp:
        check(f"3. {method} {path} is in kernel_ops._WALLED_PATHS", path in ko._WALLED_PATHS)
        r = ko._WALL_REASONS.get(path, "")
        check(f"3. {path} has the sign-in reason, naming the Network app",
              "Network app" in r and "credential" not in r, r)
    res = asyncio.run(ko.run_kernel_op({"method": "POST", "path": "/api/mcp/oauth/github/start"}))
    check("3. kernel_op POST /api/mcp/oauth/<name>/start is denied with the sign-in reason",
          res.get("status") == "denied"
          and res.get("result") == ko._WALL_REASONS.get("/api/mcp/oauth/{name}/start"), res)

    # 4: both areas reviewed
    gsrc = open(os.path.join(ROOT, "scripts", "generate_capabilities.py"), encoding="utf-8").read()
    for area in ("/api/applications/runtime", "/api/mcp"):
        rows = re.findall(r'^\s*"' + re.escape(area) + r'":\s*(\[[^\]]*\])', gsrc, re.M)
        check(f"4. _AREA_HOST_TOOLS declares {area} as [] (reviewed)", rows == ["[]"], rows)

    if FAILS:
        print(f"FAIL: {len(FAILS)} check(s)")
        return 1
    print("PASS: the Host reads kept app data, the person deletes it; MCP sign-in is the person's")
    return 0


if __name__ == "__main__":
    sys.exit(main())
