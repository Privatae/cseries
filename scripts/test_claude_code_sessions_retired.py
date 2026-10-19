#!/usr/bin/env python3
"""The claude-code app's in-container session route stays retired.

THE ROUTE (retired 2026-09-27). system-apps/claude-code is a `process` app
with no runtime.image, so its sidecar runs beside the kernel inside the Host's
container (server/applications/manifest.py runs_in_pod). Its POST /sessions
started a Claude Agent SDK session there, with shell, write and edit tools
from server/ceigas/ceigas_mcp.py running as subprocesses of the Host's own
container. Nothing in the product called it — the app UI only listed and
closed sessions, server/routes/tickets.py only closed one, and the Host's
kernel_op tool refuses any path outside /api/ — so only a direct signed-in
HTTP call could start one. The public /trust page says coding agents run in
their own container, never in the Host's; this route was the exception.

WHAT THIS HOLDS:
  * the sidecar's route table is EXACTLY the set below — a new handler fails
    this gate until it is added here on purpose, so an in-container agent
    cannot come back unnoticed under another path;
  * the library's harness-only routes (/workspace/load, /workspace/export,
    /session/export, /run/progress) are absent from it;
  * every route the app can ever have is registered at import. The pin above
    is taken from a plain import, which never runs the `if __name__ ==
    "__main__":` block (the manifest's launch does), nor a function body. So
    the source is also read: the `sidecar` object may appear ONLY as a
    top-level `@sidecar.handler(...)` decorator or as the bare
    `sidecar.serve()` in the __main__ block; any other use of it (a handler
    registered in the __main__ block or inside a function, an alias, a
    `getattr`, `_routes`) fails, as does any other import of cseries_sidecar;
  * POST /sessions and POST /sessions/continue answer 410 with a plain reason;
  * GET /status answers the connection panel's shape and nothing else;
  * importing the app runs no subprocess (it used to pip-install the SDK at
    import, before any request);
  * server/ceigas/ceigas_mcp.py — the in-container shell/write/edit tool
    server — is gone.

Drives the REAL handlers: the app module is loaded from the tree with the real
cseries_sidecar and _harness_runtime, and each route is called through the
sidecar's own route table. Serve is never called. No network, no DB, no pod.
"""
import ast
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
APP_MAIN = ROOT / "system-apps" / "claude-code" / "bin" / "main.py"

# The app's own routes. Adding one means adding it here, in the same change,
# with a reason that it runs nothing inside the Host's container.
APP_ROUTES = {
    ("GET", "/health"),              # the manifest's ready_path
    ("GET", "/status"),              # the connection panel's poll (app.js)
    ("POST", "/disconnect"),         # removes the on-disk creds (app.js)
    ("POST", "/sessions"),           # 410 Gone
    ("POST", "/sessions/continue"),  # 410 Gone
}
# Registered on EVERY sidecar that imports cseries_sidecar's shared `sidecar`
# instance (cli/cseries_sidecar.py), not by this app. None today. Pinned anyway,
# so a route the library gains later shows up here as a decision rather than
# silently.
LIBRARY_ROUTES: set = set()
# Registered by the library ONLY in a harness image (CSERIES_SIDECAR_ROLE=harness,
# cli/cseries_sidecar.py HARNESS_ONLY_ROUTES). This app runs inside the Host's
# container, where the supervisor never passes the role on, so they must be
# absent from its route table.
HARNESS_ONLY_ROUTES = {
    ("POST", "/workspace/load"),
    ("GET", "/workspace/export"),
    ("GET", "/session/export"),
    ("GET", "/run/progress"),
}

# The only names the app may take from cseries_sidecar.
ALLOWED_SIDECAR_IMPORTS = {"sidecar", "APP_ID"}

FAILED = 0


def _is_main_guard(node: ast.AST) -> bool:
    """`if __name__ == "__main__":` at module level."""
    t = getattr(node, "test", None)
    return (isinstance(node, ast.If) and isinstance(t, ast.Compare)
            and isinstance(t.left, ast.Name) and t.left.id == "__name__"
            and len(t.comparators) == 1
            and isinstance(t.comparators[0], ast.Constant)
            and t.comparators[0].value == "__main__")


def sidecar_uses_outside_the_pin(src: str) -> list[str]:
    """Every use of the `sidecar` object that the import-time route pin cannot
    see, as 'line N: why'. Empty means every registration runs at import.

    Allowed, and nothing else:
      * `@sidecar.handler(...)` decorating a MODULE-LEVEL function — it runs at
        import, so the route pin sees it;
      * `sidecar.serve()` as a bare statement directly in the module-level
        `if __name__ == "__main__":` block — the launch, which registers nothing.
    """
    tree = ast.parse(src)
    allowed: set[int] = set()   # ids of the `sidecar` Name nodes that are fine
    bad: list[str] = []

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in node.decorator_list:
                f = getattr(dec, "func", None)
                if (isinstance(f, ast.Attribute) and f.attr == "handler"
                        and isinstance(f.value, ast.Name) and f.value.id == "sidecar"):
                    allowed.add(id(f.value))
        if _is_main_guard(node):
            for stmt in node.body:
                c = getattr(stmt, "value", None)
                if (isinstance(stmt, ast.Expr) and isinstance(c, ast.Call)
                        and isinstance(c.func, ast.Attribute) and c.func.attr == "serve"
                        and isinstance(c.func.value, ast.Name)
                        and c.func.value.id == "sidecar" and not c.args and not c.keywords):
                    allowed.add(id(c.func.value))

    for n in ast.walk(tree):
        if isinstance(n, ast.Name) and n.id == "sidecar" and id(n) not in allowed:
            bad.append(f"line {n.lineno}: `sidecar` used outside a top-level "
                       f"@sidecar.handler or the __main__ sidecar.serve()")
        if isinstance(n, ast.Attribute) and n.attr == "_routes":
            bad.append(f"line {n.lineno}: touches a route table directly (_routes)")
        if isinstance(n, ast.Import) and any(a.name == "cseries_sidecar" for a in n.names):
            bad.append(f"line {n.lineno}: `import cseries_sidecar` (take names with "
                       f"`from cseries_sidecar import ...` only)")
        if isinstance(n, ast.ImportFrom) and n.module == "cseries_sidecar":
            extra = {a.name for a in n.names} - ALLOWED_SIDECAR_IMPORTS
            if extra:
                bad.append(f"line {n.lineno}: imports {sorted(extra)} from cseries_sidecar")
            if any(a.asname for a in n.names):
                bad.append(f"line {n.lineno}: aliases a cseries_sidecar import")
    return bad


def check(label: str, cond: bool, detail: str = "") -> None:
    global FAILED
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}")
    if not cond:
        FAILED += 1
        if detail:
            print(f"        {detail}")


def load_app():
    """Load the app module the way the gate needs it: from the tree, with no
    .pyc left in system-apps/ (the bundle gate would see it), no port bound,
    and every subprocess the import tries to start recorded instead of run."""
    sys.dont_write_bytecode = True
    for p in (ROOT / "cli", ROOT / "system-apps"):
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))
    # The env the app really gets: the supervisor strips CSERIES_SIDECAR_ROLE from
    # an in-container app (server/applications/supervisor.py _NOT_INHERITED_ENV),
    # and the library reads it at import.
    os.environ.pop("CSERIES_SIDECAR_ROLE", None)
    import cseries_sidecar

    # A gate must never bind a port. The app only serves under __main__, so
    # this is a guard against that regressing, not a stub the test relies on.
    cseries_sidecar.Sidecar.serve = lambda self, **kw: None
    library_routes = set(cseries_sidecar.sidecar._routes)

    spawned: list = []
    real_run = subprocess.run

    def _no_run(*a, **kw):
        spawned.append(a[0] if a else kw.get("args"))
        raise RuntimeError("test: no subprocess at import")

    subprocess.run = _no_run
    try:
        spec = importlib.util.spec_from_file_location("claude_code_app_main", APP_MAIN)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        subprocess.run = real_run
    return mod, cseries_sidecar, spawned, library_routes


def call(mod, sk, method: str, path: str, body: dict | None = None):
    """One request through the sidecar's route table, dispatched exactly as
    Sidecar's handler does it (cseries_sidecar.py: fn(req) if it takes one)."""
    fn = mod.sidecar._routes.get((method, path))
    if fn is None:
        return 404, {"error": f"no route for {method} {path}"}
    req = sk._Request(method=method, path=path, query="", headers={},
                      body=json.dumps(body).encode() if body is not None else b"")
    result = fn(req) if fn.__code__.co_argcount else fn()
    if isinstance(result, tuple) and len(result) == 2:
        payload, code = result
    else:
        payload, code = result, 200
    return code, payload


def main() -> int:
    print("── claude-code app: the in-container session route is retired ──")
    mod, sk, spawned, library_routes = load_app()

    routes = set(mod.sidecar._routes)
    check("the library registers exactly the pinned shared routes",
          library_routes == LIBRARY_ROUTES,
          f"added {sorted(library_routes - LIBRARY_ROUTES)} "
          f"missing {sorted(LIBRARY_ROUTES - library_routes)}")
    check("the sidecar's route table is exactly the pinned set",
          routes == APP_ROUTES | LIBRARY_ROUTES,
          f"unexpected {sorted(routes - APP_ROUTES - LIBRARY_ROUTES)} "
          f"missing {sorted((APP_ROUTES | LIBRARY_ROUTES) - routes)}")
    check("the harness-only workspace/session/progress routes are absent "
          "(this app runs inside the Host's container, not in a harness image)",
          not (routes & HARNESS_ONLY_ROUTES),
          f"present {sorted(routes & HARNESS_ONLY_ROUTES)}")
    bad =sidecar_uses_outside_the_pin(APP_MAIN.read_text(encoding="utf-8"))
    check("every registration is a top-level @sidecar.handler, so the pin above "
          "sees it (nothing in the __main__ block, a function body, or via _routes)",
          bad == [], "; ".join(bad))

    check("importing the app starts no subprocess (no SDK install before a request)",
          spawned == [], f"spawned: {spawned}")

    code, body = call(mod, sk, "POST", "/sessions",
                      {"prompt": "write hello.py and run it", "model": "claude-opus-5"})
    check("POST /sessions answers 410 Gone", code == 410, f"got {code}: {body}")
    reason = (body or {}).get("reason") or ""
    check("the 410 carries a plain reason naming where coding runs now",
          "harness" in reason and "build" in reason, repr(body))

    code, body = call(mod, sk, "POST", "/sessions/continue",
                      {"session_id": "s-1", "message": "keep going"})
    check("POST /sessions/continue answers 410 Gone", code == 410, f"got {code}: {body}")

    # The connection panel's poll. Pin the token lookup so the gate never reads
    # this machine's own CLI credentials or refreshes anything over the network.
    mod._get_token = lambda: None
    code, body = call(mod, sk, "GET", "/status")
    check("GET /status answers the connection shape only (no sessions, no runs)",
          code == 200 and set(body) == {"connected", "expires_at", "connected_at"}
          and body["connected"] is False, f"got {code}: {body}")

    check("server/ceigas/ceigas_mcp.py (the in-container shell/write/edit tools) is gone",
          not (ROOT / "server" / "ceigas" / "ceigas_mcp.py").exists())

    print()
    if FAILED:
        print(f"FAIL — {FAILED} check(s) failed")
        return 1
    print("PASS — the in-container session route stays retired")
    return 0


if __name__ == "__main__":
    sys.exit(main())
