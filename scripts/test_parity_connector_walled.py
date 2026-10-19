#!/usr/bin/env python3
"""The /api/connector area is WALLED from the Host, and its card says who calls it.

server/routes/connector.py's routes are a messaging connector app's own way in,
opened only by the capability issued to that app. No host verb calls them: POST
/pair carries the one-time code that must never reach the model, and POST
/messages starts the person's turns. So the host-parity ledger classes them
walled (kernel_ops._WALLED_PATHS), never covered by the NAC tools that govern
the area: those tools reach none of these routes.

Checks, against the real modules:
  1. every route the connector router registers is in kernel_ops._WALLED_PATHS
     (read from the router's own decorators, so a seventh route fails here;
     the sixth is the Discord gateway's WebSocket, @router.websocket);
  2. kernel_op refuses each one with the connector reason, not the default
     "returns credential bytes" text, and a concrete /turns/<id> is walled by
     its template;
  3. generate_capabilities declares the area [] (reviewed, no host tool), so
     with the wall the routes derive walled rather than unreachable;
  4. the area card for a walled area carries _AREA_PERSON_NOTE (the tools that
     govern it) instead of "the user drives it in the app".
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


def check(name: str, ok: bool, detail=None) -> None:
    print(("ok   " if ok else "FAIL ") + name)
    if not ok:
        FAILS.append(name)
        if detail is not None:
            print(f"     {detail!r}"[:600])


def connector_routes() -> list[tuple[str, str]]:
    """(METHOD, path) for every @router.<method>(...) in server/routes/connector.py."""
    src = open(os.path.join(ROOT, "server", "routes", "connector.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    prefix = None
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
                        and getattr(d.func.value, "id", None) == "router":
                    out.append((d.func.attr.upper(), prefix + ast.literal_eval(d.args[0])))
    return out


def main() -> int:
    routes = connector_routes()
    check("0. the connector router registers its six routes under /api/connector "
          "(five HTTP, and the Discord gateway's WebSocket)",
          len(routes) == 6 and all(p.startswith("/api/connector/") for _m, p in routes)
          and ("WEBSOCKET", "/api/connector/gateway") in routes, routes)

    from mastercode.runtime import kernel_ops as ko
    for method, path in routes:
        check(f"1. {method} {path} is in kernel_ops._WALLED_PATHS", path in ko._WALLED_PATHS)
        check(f"2. {method} {path} is not an exposed kernel op", ko.exposed_route(method, path) is None)
        reason = ko._WALL_REASONS.get(path, "")
        check(f"2. {path} has its own wall reason, naming the governing tools",
              "credential" not in reason and "propose_nac_policy" in reason
              and "revoke_nac_rule" in reason, reason)
    check("2. a concrete /turns/<id> is walled by its template",
          ko._walled_path_for("/api/connector/turns/t-123") == "/api/connector/turns/{turn_id}")
    res = asyncio.run(ko.run_kernel_op({"method": "GET", "path": "/api/connector/gateway"}))
    check("2. kernel_op GET /api/connector/gateway (the gateway socket's path) is denied with the "
          "connector reason",
          res.get("status") == "denied"
          and res.get("result") == ko._WALL_REASONS.get("/api/connector/gateway"), res)
    res = asyncio.run(ko.run_kernel_op({"method": "POST", "path": "/api/connector/messages"}))
    check("2. kernel_op POST /api/connector/messages is denied with the connector reason",
          res.get("status") == "denied"
          and res.get("result") == ko._WALL_REASONS.get("/api/connector/messages"), res)

    gsrc = open(os.path.join(ROOT, "scripts", "generate_capabilities.py"), encoding="utf-8").read()
    rows = re.findall(r'^\s*"/api/connector":\s*(\[[^\]]*\])', gsrc, re.M)
    check("3. _AREA_HOST_TOOLS declares /api/connector as [] (reviewed, no host tool)",
          rows == ["[]"], rows)

    import generate_capabilities as g
    note = g._AREA_PERSON_NOTE.get("/api/connector", "")
    check("4. the area note names the tools that govern the area",
          all(t in note for t in ("propose_nac_policy", "nac_status", "revoke_nac_rule")), note)
    eps = [{"method": m, "path": p, "scope": None, "does": ""} for m, p in routes]
    card = g._area_card({"prefix": "/api/connector", "scopes": []}, eps, "", [], "walled")
    check("4. the walled card carries the note", note and note in card["how"], card["how"][:400])
    check("4. the walled card does not say the user drives it in the app",
          "the user drives it in the app" not in card["how"], card["how"][:400])
    other = g._area_card({"prefix": "/api/nothing-noted", "scopes": []},
                         [{"method": "GET", "path": "/api/nothing-noted/x", "scope": None, "does": ""}],
                         "", [], "walled")
    check("4. a walled area with no note keeps the generic sentence",
          "the user drives it in the app" in other["how"], other["how"][:300])

    if FAILS:
        print(f"FAIL: {len(FAILS)} check(s)")
        return 1
    print("PASS: /api/connector is walled from the Host and its card names the tools that govern it")
    return 0


if __name__ == "__main__":
    sys.exit(main())
