#!/usr/bin/env python3
"""The Git server routes and naming the Host are classified for the Host.

Settings, Git server (server/routes/git_server.py): GET what is connected,
POST connect, DELETE disconnect. Connecting decides which server and which
account the Host works as, so it is the owner's act; the Host's git work reads
the connection in-process (git_server.forge_identity), never through these
routes. Naming the Host (server/routes/host_name.py, POST /api/system/name):
the name the Host is called by is the owner's choice, in Settings, About.

Without a classification each route counts as unreviewed, and the host-parity
ratchet in the release smoke (scripts/check_host_parity.py) stops above its
floor.

Checks, against the real modules:
  1. git_server.py registers GET, POST and DELETE /api/git-server and nothing
     else (read from the router's own decorators, so a new route fails here);
     their scopes carry no entity bits, /api/git-server is not exposed to
     kernel_op, and _AREA_HOST_TOOLS declares the area [] (reviewed), so all
     three derive WALLED;
  2. host_name.py registers POST /api/system/name; write:host_name carries no
     entity bits; the path is in kernel_ops._WALLED_PATHS with its own reason
     naming Settings, About (not the credential text), and kernel_op refuses
     it with that reason.
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

GIT_SERVER = "/api/git-server"
HOST_NAME = "/api/system/name"


def check(name: str, ok: bool, detail=None) -> None:
    print(("ok   " if ok else "FAIL ") + name)
    if not ok:
        FAILS.append(name)
        if detail is not None:
            print(f"     {detail!r}"[:600])


def router_routes(rel: str) -> list[tuple[str, str, str | None]]:
    """(METHOD, full path, scope) for every @router.<method>("...") in one
    route module; scope is the requires_permission("...") argument, if any."""
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
                    scope = None
                    for c in ast.walk(d):
                        if isinstance(c, ast.Call) and getattr(c.func, "id", None) == "requires_permission" \
                                and c.args:
                            scope = ast.literal_eval(c.args[0])
                    out.append((d.func.attr.upper(), prefix + ast.literal_eval(d.args[0]), scope))
    return out


def main() -> int:
    from mastercode.runtime import kernel_ops as ko
    from mastercode.runtime.meta_tool_caps import ENTITY_SCOPE_TO_BITS

    def exposed(path: str) -> bool:
        return any(path == p or path.startswith(p + "/") for p in ko._EXPOSED_PREFIXES)

    # 1: Settings, Git server
    gs = router_routes("server/routes/git_server.py")
    check("1. git_server.py registers GET, POST and DELETE /api/git-server, nothing else",
          sorted((m, p) for m, p, _s in gs) == [("DELETE", GIT_SERVER), ("GET", GIT_SERVER),
                                                ("POST", GIT_SERVER)], gs)
    for method, path, scope in gs:
        check(f"1. {method} {path} declares a scope with no entity bits ({scope})",
              scope is not None and not ENTITY_SCOPE_TO_BITS.get(scope, 0))
    check("1. /api/git-server is not exposed to kernel_op", not exposed(GIT_SERVER))
    gsrc = open(os.path.join(ROOT, "scripts", "generate_capabilities.py"), encoding="utf-8").read()
    rows = re.findall(r'^\s*"' + re.escape(GIT_SERVER) + r'":\s*(\[[^\]]*\])', gsrc, re.M)
    check("1. _AREA_HOST_TOOLS declares /api/git-server as [] (reviewed)", rows == ["[]"], rows)

    # 2: naming the Host
    hn = router_routes("server/routes/host_name.py")
    check("2. host_name.py registers POST /api/system/name, nothing else",
          [(m, p) for m, p, _s in hn] == [("POST", HOST_NAME)], hn)
    check("2. write:host_name has no entity bits",
          bool(hn) and hn[0][2] == "write:host_name" and not ENTITY_SCOPE_TO_BITS.get("write:host_name", 0),
          hn)
    check("2. POST /api/system/name is walled by path", HOST_NAME in ko._WALLED_PATHS)
    reason = ko._WALL_REASONS.get(HOST_NAME, "")
    check("2. its reason names Settings, About, not the credential text",
          "Settings, About" in reason and "credential" not in reason, reason)
    saved = ko._contract_cache
    ko._contract_cache = {("POST", HOST_NAME): {"method": "POST", "path": HOST_NAME,
                                                "scope": "write:host_name"}}
    try:
        res = asyncio.run(ko.run_kernel_op({"method": "POST", "path": HOST_NAME,
                                            "body": {"name": "Juniper"}}))
        check("2. kernel_op POST /api/system/name is denied with that reason",
              res.get("status") == "denied" and res.get("result") == reason, res)
    finally:
        ko._contract_cache = saved

    if FAILS:
        print(f"FAIL: {len(FAILS)} check(s)")
        return 1
    print("PASS: connecting a git server and naming the Host are the owner's, in Settings")
    return 0


if __name__ == "__main__":
    sys.exit(main())
