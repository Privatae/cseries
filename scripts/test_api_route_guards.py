#!/usr/bin/env python3
"""Gate: every /api route requires an app-token permission, or says why not.

WHY. POST /api/agents/dispatch had no permission check — not on the
route, not on its router, not on the include_router — and nothing compared the
route table with the permission model, so nothing noticed. Any app on the Host
could start a scoped agent with the tool scope and capability grant of its
choosing. An origin-isolated community app reaches it too: its own /api calls
land on the same pod (server/routes/applications.py, shell_token). The
middleware does not close this: in its default `audit` mode a token-less call
is only logged, and `strict` asks for SOME token, which every app window has.
A probe got 200 with no token, with the shell's read-only token and
with a zero-permission app token. The same sweep found the same hole on other
routes; they are scoped, the ones nothing calls are removed, and this gate holds it.

WHAT IT CHECKS, against FastAPI's BUILT route table (server.main.app, every
include_router / add_api_route / app-level route / WebSocket route — never the
source text):

  1. Every /api route depends on requires_permission(<scope>), or is listed in
     server/applications/route_policy.py UNSCOPED_ROUTES with a kind and a
     reason. Anything else is UNGUARDED and fails, by name.
  2. Listed entries are honest: each matches a live route; a scoped route is not
     also listed; bootstrap/public/stream entries are GET-only; a `shell-origin`
     entry really carries the shell_origin_when_tokenless dependency, and every
     route that carries it is listed as `shell-origin`.
  3. No Mount under /api (the gate could not see inside it).
  4. The routes this PR removed stay removed (POST /api/agents/dispatch, the
     genesis link routes).
  5. Behaviour, through the REAL ApplicationTokenMiddleware (audit AND strict)
     and each route's REAL guard dependency, with only the endpoint stubbed:
     every route this PR guarded refuses no token (401), the shell's own token
     and a zero-permission app token (403), and admits a token holding its
     scope. Shell-origin routes refuse a token-less isolated-app origin and a
     cross-origin browser, and admit the shell's own page.
  6. Mutation: stripping any one of those guards — or re-adding an unguarded
     POST /api/agents/dispatch — makes check 1 fail on exactly that route.

Container-free: imports server.main the way scripts/test_app_manifest_scopes.py
does (brain/ on the path), with StaticFiles' directory check off so the
dashboard mount does not need /app/dashboard. No DB, no network, no pod.

Run: APP_ROOT=<repo> python3 scripts/test_api_route_guards.py [--list]
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "brain"))

_TMP = tempfile.TemporaryDirectory(prefix="api-route-guards-")
_SCRATCH = _TMP.name
# server.routes.training_memory creates its jobs dir at import.
os.environ.setdefault("TM_JOBS_ROOT", os.path.join(_SCRATCH, "tm-jobs"))

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(("  OK   " if ok else "  FAIL ") + label + (f"\n         {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(label)


# ── the route table ─────────────────────────────────────────────────────────

@dataclass
class RouteInfo:
    path: str
    methods: tuple[str, ...]          # ("WEBSOCKET",) for a socket
    calls: list = field(default_factory=list)   # every dependency callable, flattened
    endpoint: str = ""
    kind: str = ""                    # the route object's class name

    @property
    def scopes(self) -> list[str]:
        from server.applications.api_contract import SCOPE_ATTR
        return [getattr(c, SCOPE_ATTR) for c in self.calls if getattr(c, SCOPE_ATTR, None)]

    @property
    def shell_origin(self) -> bool:
        from server.applications.route_policy import SHELL_ORIGIN_ATTR
        return any(getattr(c, SHELL_ORIGIN_ATTR, False) for c in self.calls)

    @property
    def guard_calls(self) -> list:
        from server.applications.api_contract import SCOPE_ATTR
        from server.applications.route_policy import SHELL_ORIGIN_ATTR
        return [c for c in self.calls
                if getattr(c, SCOPE_ATTR, None) or getattr(c, SHELL_ORIGIN_ATTR, False)]


def load_app():
    """server.main's live FastAPI app, imported without the container's dirs."""
    import starlette.staticfiles as _sf
    orig = _sf.StaticFiles.__init__

    def _init(self, *a, **k):
        k["check_dir"] = False
        return orig(self, *a, **k)
    _sf.StaticFiles.__init__ = _init
    try:
        from server.main import app
    finally:
        _sf.StaticFiles.__init__ = orig
    return app


def _flat_calls(dependant) -> list:
    out, stack, seen = [], list(getattr(dependant, "dependencies", None) or []), 0
    while stack and seen < 500:
        seen += 1
        d = stack.pop()
        if getattr(d, "call", None) is not None:
            out.append(d.call)
        stack.extend(getattr(d, "dependencies", None) or [])
    return out


def effective_routes(app) -> tuple[list[RouteInfo], list[str]]:
    """Every route FastAPI will dispatch to, with its effective path and its
    full dependency set (router- and include-level dependencies included).

    Two FastAPI shapes: the image pins 0.133, where include_router copies each
    route onto app.routes with the combined prefix and dependencies; newer
    releases keep an _IncludedRouter and resolve effective routes lazily
    (fastapi.routing.iter_route_contexts). Both are read; an unexpanded
    included router is reported, never skipped."""
    from starlette.routing import Mount, WebSocketRoute
    problems: list[str] = []
    raw = []
    try:
        from fastapi.routing import iter_route_contexts  # newer FastAPI
    except ImportError:
        iter_route_contexts = None
    if iter_route_contexts is None:
        raw = list(app.routes)
    else:
        for rc in iter_route_contexts(app.routes):
            ctx = getattr(rc, "_route_context", None)
            if ctx is None:
                raw.append(rc.route)
            elif getattr(ctx, "starlette_route", None) is not None:
                raw.append(ctx.starlette_route)
            else:
                raw.append(ctx)
    out: list[RouteInfo] = []
    for r in raw:
        cls = type(r).__name__
        if "IncludedRouter" in cls:
            problems.append(f"an included router was not expanded ({cls}) — the gate cannot see its routes")
            continue
        path = getattr(r, "path", "") or ""
        if isinstance(r, Mount):
            if path.startswith("/api"):
                problems.append(f"a Mount at {path!r} — the gate cannot see the routes inside it")
            continue
        if isinstance(r, WebSocketRoute) or "WebSocket" in cls:
            methods: tuple[str, ...] = ("WEBSOCKET",)
        else:
            methods = tuple(sorted(m for m in (getattr(r, "methods", None) or ())
                                   if m not in ("HEAD", "OPTIONS")))
        ep = getattr(r, "endpoint", None)
        out.append(RouteInfo(
            path=path, methods=methods, calls=_flat_calls(getattr(r, "dependant", None)),
            endpoint=(f"{getattr(ep, '__module__', '?')}:{getattr(ep, '__qualname__', '?')}" if ep else ""),
            kind=cls,
        ))
    return out, problems


# ── the rule ────────────────────────────────────────────────────────────────

def classify(routes: list[RouteInfo], policy: dict) -> tuple[dict, list[str]]:
    """(rows, problems). rows maps (METHOD, path) -> class."""
    from server.applications.route_policy import KINDS, READ_ONLY_KINDS
    rows: dict[tuple[str, str], str] = {}
    problems: list[str] = []
    for r in routes:
        if not r.path.startswith("/api/"):
            continue
        for m in r.methods:
            key = (m, r.path)
            listed = policy.get(key)
            where = f"{m} {r.path}  [{r.endpoint}]"
            if m == "WEBSOCKET" and (r.scopes or r.shell_origin):
                # ApplicationTokenMiddleware handles HTTP only, and both
                # requires_permission and shell_origin_when_tokenless need an HTTP
                # request, so on a socket the dependency raises for every caller. A
                # socket must authenticate itself and be listed with a reason.
                guard = (f"requires_permission ({r.scopes})" if r.scopes
                         else "shell_origin_when_tokenless")
                problems.append(f"{where}: a WebSocket route relies on {guard}, an HTTP-only guard "
                                f"that cannot work on a socket — authenticate it in the handler and "
                                f"list it in route_policy.UNSCOPED_ROUTES with a reason")
                rows[key] = "guarded-websocket"
                continue
            if r.shell_origin:
                if not listed or listed[0] != "shell-origin":
                    problems.append(f"{where}: carries shell_origin_when_tokenless but is not listed "
                                    f"as 'shell-origin' in route_policy.UNSCOPED_ROUTES")
                rows[key] = "shell-origin"
                continue
            if r.scopes:
                if listed:
                    problems.append(f"{where}: requires {r.scopes} AND is listed as {listed[0]!r} — "
                                    f"drop the route_policy entry")
                rows[key] = "scoped"
                continue
            if listed:
                kind, reason = listed
                if kind not in KINDS:
                    problems.append(f"{where}: unknown kind {kind!r}")
                if not (reason or "").strip():
                    problems.append(f"{where}: listed with no reason")
                if kind in READ_ONLY_KINDS and m != "GET":
                    problems.append(f"{where}: kind {kind!r} is read-only, but this is {m}")
                if kind == "shell-origin":
                    problems.append(f"{where}: listed as 'shell-origin' but the route has no "
                                    f"shell_origin_when_tokenless dependency")
                rows[key] = kind
                continue
            problems.append(f"UNGUARDED {where}: no requires_permission and no route_policy entry")
            rows[key] = "UNGUARDED"
    for key in policy:
        if key not in rows:
            problems.append(f"stale route_policy entry {key[0]} {key[1]}: no such route")
    return rows, problems


# ── what this PR changed, pinned ────────────────────────────────────────────

NEWLY_SCOPED: list[tuple[str, str, str]] = [
    ("GET", "/api/agents", "read:brain_tasks"),
    ("GET", "/api/agents/{task_id}/stream", "read:brain_tasks"),
    ("POST", "/api/agents/{task_id}/cancel", "write:brain_tasks"),
    ("GET", "/api/agents/scheduled", "read:calendar"),
    ("POST", "/api/agents/scheduled/{event_id}/cancel", "write:calendar"),
    ("GET", "/api/appearance/network", "read:topology"),
    ("GET", "/api/appearance/network/meta", "read:topology"),
    ("GET", "/api/applications/publisher/my-apps", "publish:apps"),
    ("GET", "/api/applications/publisher/sales", "publish:apps"),
    ("GET", "/api/applications/sites/list", "read:sites"),
    ("GET", "/api/applications/sites/analytics", "read:sites"),
    ("GET", "/api/applications/sites/handle", "read:sites"),
    ("GET", "/api/applications/sites/domains", "read:sites"),
    ("GET", "/api/brain/turns/active", "read:brain_memory"),
    ("GET", "/api/brain/turns/{turn_id}/stream", "read:brain_memory"),
    ("POST", "/api/brain/turns/{turn_id}/cancel", "write:session"),
    ("GET", "/api/review/sessions", "read:review"),
    ("GET", "/api/skills/list", "read:files"),
    ("GET", "/api/skills/{name}", "read:files"),
    # The Host's own skills. The
    # name is a path parameter: a skill v207 moved in keeps its file's path as
    # its name (`nested/thing`).
    ("GET", "/api/skills/host/{name:path}", "read:files"),
    ("GET", "/api/skills/host/{name:path}/versions/{version:int}", "read:files"),
    ("PUT", "/api/skills/host/{name:path}", "write:files"),
    ("POST", "/api/skills/host/{name:path}/retire", "write:files"),
    ("POST", "/api/skills/host/{name:path}/restore", "write:files"),
    ("GET", "/api/system/errors/recent", "read:system"),
    ("GET", "/api/training-memory/existing-roles", "read:brain_corrections"),
    ("POST", "/api/training-memory/jobs", "write:brain_corrections"),
    ("POST", "/api/training-memory/jobs/from-sdk-sessions", "write:brain_corrections"),
    ("GET", "/api/training-memory/jobs/{job_id}/estimate", "read:brain_corrections"),
    ("POST", "/api/training-memory/jobs/{job_id}/start", "write:brain_corrections"),
    ("GET", "/api/training-memory/jobs/{job_id}/result", "read:brain_corrections"),
    ("GET", "/api/training-memory/jobs/{job_id}/review", "read:brain_corrections"),
    ("PATCH", "/api/training-memory/jobs/{job_id}/review/{pair_id}", "write:brain_corrections"),
    ("POST", "/api/training-memory/jobs/{job_id}/review/bulk", "write:brain_corrections"),
    ("POST", "/api/training-memory/jobs/{job_id}/commit", "write:brain_corrections"),
    ("POST", "/api/training-memory/jobs/{job_id}/undo", "write:brain_corrections"),
]
# (METHOD, path, scope a token must hold, or None = the handler decides)
NEWLY_SHELL_ORIGIN: list[tuple[str, str, str | None]] = [
    ("POST", "/api/applications/{app_id}/close", None),
    ("POST", "/api/applications/{app_id}/window-beat", None),
    ("POST", "/api/applications/{app_id}/runtime", None),
    ("PUT", "/api/appearance/layout", "write:appearance"),
    ("POST", "/api/appearance/desktop/loop", "write:appearance"),
]
REMOVED: list[tuple[str, str]] = [
    ("POST", "/api/agents/dispatch"),
    ("GET", "/api/genesis/status"),
    ("POST", "/api/genesis/link"),
    # The Skills app's folder picker. Its GET
    # now reads as the platform skill named "location" (GET /api/skills/{name}).
    ("POST", "/api/skills/location"),
    # Circles' Regenerate, A/B and their job route, and the Activity feed,
    # left with the rail.
    ("POST", "/api/circles/replay"),
    ("POST", "/api/circles/ab"),
    ("POST", "/api/circles/ab/choose"),
    ("GET", "/api/circles/jobs/{job_id}"),
    ("GET", "/api/circles/activity"),
]
# Routes that take any ONE of several scopes (requires_any_permission): the
# narrow one first, then the wide one existing callers hold. Request
# connection is all the Network app does with contacts.
ANY_OF: list[tuple[str, str, tuple[str, ...]]] = [
    ("POST", "/api/contacts/request-peer", ("request:peer", "write:address_book")),
    # Here the WIDE one is first: disconnect:own_harness opens this route only for the
    # calling engine app's own sign-in (routes/vault.py _own_harness_only), so the
    # route's scope in the API contract stays write:vault.
    ("DELETE", "/api/vault/secrets/{name}", ("write:vault", "disconnect:own_harness")),
]


def _rx(template: str) -> re.Pattern:
    from starlette.routing import compile_path
    return compile_path(template)[0]


def _resolve(routes: list[RouteInfo], method: str, concrete: str) -> RouteInfo | None:
    """The first route (dispatch order) that would serve method + path."""
    for r in routes:
        if method in r.methods and _rx(r.path).match(concrete):
            return r
    return None


def _concrete(template: str) -> str:
    return re.sub(r"\{[^}]+\}", "1", template)


# ── behaviour: the real middleware, the real guards, a stub endpoint ────────

SHELL_HOST = "alex.host.example.test"
APPS_HOST = "apps-alex.host.example.test"


def _build_probe_app(guarded: list[tuple[str, RouteInfo]], secret: bytes):
    from fastapi import APIRouter, Depends, FastAPI
    from server.applications.middleware import ApplicationTokenMiddleware

    async def _stub():
        return {"reached": True}

    app = FastAPI()
    app.add_middleware(ApplicationTokenMiddleware)
    app.state.kernel_secret = secret
    router = APIRouter()
    for method, info in guarded:
        router.add_api_route(info.path, _stub, methods=[method],
                             dependencies=[Depends(c) for c in info.guard_calls])
    app.include_router(router)
    return app


async def _call(app, method: str, path: str, headers: dict) -> int:
    import httpx
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url=f"http://{SHELL_HOST}") as c:
        kw = {"json": {}} if method in ("POST", "PUT", "PATCH") else {}
        resp = await c.request(method, path, headers=headers, **kw)
        return resp.status_code


def behaviour(routes: list[RouteInfo]) -> None:
    import logging
    # Every probe below that carries no token is a would-deny the middleware
    # logs at WARNING; that is the expected traffic here, not a finding.
    logging.getLogger("privatae.applications.middleware").setLevel(logging.ERROR)
    from server.applications import middleware as mw
    from server.applications import tokens
    from server.applications.scopes import PERMISSIONS
    from server.routes.applications import SHELL_APP_ID, SHELL_SCOPES

    secret = b"route-guard-gate-secret-32bytes!"
    shell_tok = tokens.mint(secret, app_id=SHELL_APP_ID, permissions=list(SHELL_SCOPES))[0]
    zero_tok = tokens.mint(secret, app_id="community-app", permissions=[])[0]
    every_tok = tokens.mint(secret, app_id="every-scope", permissions=sorted(PERMISSIONS))[0]

    def tok(scope):
        return tokens.mint(secret, app_id="some-app", permissions=[scope])[0]

    shell_page = {"x-forwarded-host": SHELL_HOST, "origin": f"https://{SHELL_HOST}",
                  "sec-fetch-site": "same-origin"}
    isolated = {"x-forwarded-host": APPS_HOST, "origin": f"https://{APPS_HOST}",
                "sec-fetch-site": "same-origin"}
    cross_to_shell = {"x-forwarded-host": SHELL_HOST, "origin": f"https://{APPS_HOST}",
                      "sec-fetch-site": "same-site"}

    by_key = {}
    for r in routes:
        for m in r.methods:
            by_key.setdefault((m, r.path), r)

    guarded: list[tuple[str, RouteInfo]] = []
    for m, p, scope in NEWLY_SCOPED:
        info = by_key.get((m, p))
        check(f"{m} {p} exists and requires {scope}",
              info is not None and info.scopes == [scope] and not info.shell_origin,
              f"found {info.scopes if info else 'no route'}")
        if info is not None:
            guarded.append((m, info))
    for m, p, scope in NEWLY_SHELL_ORIGIN:
        info = by_key.get((m, p))
        check(f"{m} {p} carries the shell-origin guard" + (f" + {scope}" if scope else ""),
              info is not None and info.shell_origin and info.scopes == ([scope] if scope else []),
              f"found shell_origin={info.shell_origin if info else None} scopes={info.scopes if info else None}")
        if info is not None:
            guarded.append((m, info))

    app = _build_probe_app(guarded, secret)
    scope_of = {(m, p): s for m, p, s in NEWLY_SCOPED}
    shell_scope_of = {(m, p): s for m, p, s in NEWLY_SHELL_ORIGIN}

    async def run_all():
        for mode in ("audit", "strict"):
            saved = mw._MODE
            mw._MODE = mode
            try:
                for m, info in guarded:
                    url = _concrete(info.path)
                    key = (m, info.path)
                    if key in scope_of:
                        scope = scope_of[key]
                        st_none = await _call(app, m, url, dict(shell_page))
                        st_shell = await _call(app, m, url, {**shell_page, "X-Application-Token": shell_tok})
                        st_zero = await _call(app, m, url, {**isolated, "X-Application-Token": zero_tok})
                        st_ok = await _call(app, m, url, {**isolated, "X-Application-Token": tok(scope)})
                        # The shell token holds read:calendar, and /api/calendar/list
                        # already serves it every scheduled_task row, so it opening
                        # /api/agents/scheduled is the same read, not a new one.
                        want_shell = 200 if scope in SHELL_SCOPES else 403
                        check(f"[{mode}] {m} {info.path}: no token 401, shell token {want_shell}, "
                              f"zero-permission token 403, {scope} 200",
                              (st_none, st_shell, st_zero, st_ok) == (401, want_shell, 403, 200),
                              f"got none={st_none} shell={st_shell} zero={st_zero} scoped={st_ok}")
                    else:
                        scope = shell_scope_of[key]
                        st_iso = await _call(app, m, url, dict(isolated))
                        st_cross = await _call(app, m, url, dict(cross_to_shell))
                        st_shell_page = await _call(app, m, url, dict(shell_page))
                        st_script = await _call(app, m, url, {})   # curl / smoke: no browser headers
                        st_zero = await _call(app, m, url, {**isolated, "X-Application-Token": zero_tok})
                        want_zero = 403 if scope else 200
                        ok = (st_iso, st_cross, st_shell_page, st_script, st_zero) == (403, 403, 200, 200, want_zero)
                        detail = (f"got isolated={st_iso} cross={st_cross} shell-page={st_shell_page} "
                                  f"script={st_script} zero-token={st_zero}")
                        if scope:
                            st_ok = await _call(app, m, url, {**isolated, "X-Application-Token": tok(scope)})
                            ok = ok and st_ok == 200
                            detail += f" {scope}={st_ok}"
                        check(f"[{mode}] {m} {info.path}: token-less refused off the shell's page "
                              f"(isolated 403, cross-origin 403), admitted on it"
                              + (f"; a token needs {scope}" if scope else "; a token is the handler's call"),
                              ok, detail)
            finally:
                mw._MODE = saved

        # The dispatch route is GONE, for every caller — a token holding every
        # scope in the catalogue included.
        from fastapi import FastAPI
        from server.applications.middleware import ApplicationTokenMiddleware
        from server.routes import agents as agents_routes
        real = FastAPI()
        real.add_middleware(ApplicationTokenMiddleware)
        real.state.kernel_secret = secret
        real.include_router(agents_routes.router)
        body = {"title": "t", "task_prompt": "p", "tool_scope": ["web_search"], "grant": ["x"]}
        import httpx
        transport = httpx.ASGITransport(app=real, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url=f"http://{SHELL_HOST}") as c:
            for label, hdr in (("no token", {}), ("shell token", {"X-Application-Token": shell_tok}),
                               ("zero-permission token", {"X-Application-Token": zero_tok}),
                               ("a token with every scope", {"X-Application-Token": every_tok})):
                st = (await c.post("/api/agents/dispatch", json=body, headers=hdr)).status_code
                check(f"POST /api/agents/dispatch with {label} reaches no handler ({st})", st in (404, 405))

    asyncio.run(run_all())
    any_of_behaviour(by_key, secret, tok, shell_tok, zero_tok)
    resources_behaviour(secret, tok, zero_tok, every_tok)


def any_of_behaviour(by_key: dict, secret: bytes, tok, shell_tok: str, zero_tok: str) -> None:
    """A route that takes any one of several scopes: each of them admits it,
    nothing else does, in both middleware modes; and the API contract carries
    them all, so a manifest holding only the narrow one passes conformance."""
    from server.applications import middleware as mw
    from server.applications.api_contract import SCOPES_ATTR, _route_scopes
    for m, p, scopes in ANY_OF:
        info = by_key.get((m, p))
        stamped = [tuple(getattr(c, SCOPES_ATTR, ()) or ()) for c in (info.calls if info else [])
                   if getattr(c, SCOPES_ATTR, None)]
        check(f"{m} {p} takes any of {', '.join(scopes)} (its scope in the contract: {scopes[0]})",
              info is not None and info.scopes == [scopes[0]] and stamped == [scopes] and not info.shell_origin,
              f"found scopes={info.scopes if info else None} any-of={stamped}")
        if info is None:
            continue
        app = _build_probe_app([(m, info)], secret)
        url = _concrete(info.path)
        isolated = {"x-forwarded-host": APPS_HOST, "origin": f"https://{APPS_HOST}",
                    "sec-fetch-site": "same-origin"}

        async def run(mode):
            saved = mw._MODE
            mw._MODE = mode
            try:
                got = {
                    "none": await _call(app, m, url, dict(isolated)),
                    "shell": await _call(app, m, url, {**isolated, "X-Application-Token": shell_tok}),
                    "zero": await _call(app, m, url, {**isolated, "X-Application-Token": zero_tok}),
                    "other": await _call(app, m, url, {**isolated, "X-Application-Token": tok("read:address_book")}),
                }
                for sc in scopes:
                    got[sc] = await _call(app, m, url, {**isolated, "X-Application-Token": tok(sc)})
                return got
            finally:
                mw._MODE = saved

        for mode in ("audit", "strict"):
            got = asyncio.run(run(mode))
            want = {"none": 401, "shell": 403, "zero": 403, "other": 403, **{sc: 200 for sc in scopes}}
            check(f"[{mode}] {m} {p}: no token 401, the shell's and a zero-permission or read-only token 403, "
                  f"{' or '.join(scopes)} 200", got == want, f"got {got}")
        # The contract's own reader (build_api_contract reads each route
        # through _route_scopes), on the route as its router declares it.
        from server.applications.api_contract import _route_scope
        from server.routes import contacts as contacts_routes
        from server.routes import vault as vault_routes
        route = next((r for rt in (contacts_routes.router, vault_routes.router) for r in rt.routes
                      if getattr(r, "path", "") == p and m in (getattr(r, "methods", None) or ())), None)
        check(f"{m} {p}: the API contract names its scope and every scope that admits it",
              route is not None and _route_scope(route) == scopes[0] and _route_scopes(route) == list(scopes),
              f"{(_route_scope(route), _route_scopes(route)) if route else 'no such route on the contacts or vault router'}")


def resources_behaviour(secret: bytes, tok, zero_tok: str, every_tok: str) -> None:
    """GET /api/system/resources: command-line text only for read:system."""
    from fastapi import FastAPI
    from server.applications.middleware import ApplicationTokenMiddleware
    from server.routes import system as system_routes
    import httpx

    app = FastAPI()
    app.add_middleware(ApplicationTokenMiddleware)
    app.state.kernel_secret = secret
    app.include_router(system_routes.router)

    async def names(headers: dict) -> tuple[int, list[str]]:
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url=f"http://{SHELL_HOST}") as c:
            r = await c.get("/api/system/resources", headers=headers)
            top = r.json()["processes"]["top"] if r.status_code == 200 else []
            return r.status_code, [p["name"] for p in top]

    import time as _time
    stats = Path(_SCRATCH) / "container-stats"
    stats.write_text(f"ts {int(_time.time())}\n"
                     "proc 11 5000 postgres: walwriter   \n"
                     "proc 12 4000 /usr/lib/postgresql/17/bin/postgres -D /pgdata -c secret=1\n")
    system_routes._DB_STATS = stats

    async def db_names(headers: dict) -> list[str]:
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url=f"http://{SHELL_HOST}") as c:
            r = await c.get("/api/system/resources", headers=headers)
            return [p["name"] for p in r.json()["db"]["processes"]["top"]]

    async def go():
        for label, hdr in (("no token", {}), ("a token without read:system", {"X-Application-Token": zero_tok})):
            dn = await db_names(hdr)
            check(f"GET /api/system/resources db block, {label}: process names only",
                  sorted(dn) == ["postgres", "postgres"], str(dn))
        dn = await db_names({"X-Application-Token": tok("read:system")})
        check("GET /api/system/resources db block, read:system: command lines",
              any("walwriter" in n for n in dn) and any("-D /pgdata" in n for n in dn), str(dn))
        st, none_names = await names({})
        check("GET /api/system/resources: no token -> 200 with process names only (no command lines)",
              st == 200 and bool(none_names) and all(" " not in n and "/" not in n for n in none_names),
              f"{st} {none_names[:3]}")
        st, zero_names = await names({"X-Application-Token": zero_tok})
        check("GET /api/system/resources: a token without read:system gets names only",
              st == 200 and bool(zero_names) and all(" " not in n and "/" not in n for n in zero_names),
              f"{st} {zero_names[:3]}")
        st, full_names = await names({"X-Application-Token": tok("read:system")})
        check("GET /api/system/resources: read:system gets command lines",
              st == 200 and any(" " in n for n in full_names), f"{st} {full_names[:3]}")
    asyncio.run(go())


# ── mutation: strip a guard, the rule must name that route ───────────────────

def mutations(routes: list[RouteInfo], policy: dict) -> None:
    targets = [(m, p) for m, p, _ in NEWLY_SCOPED] + [(m, p) for m, p, _ in NEWLY_SHELL_ORIGIN]
    for m, p in targets:
        mutated = []
        for r in routes:
            if r.path == p and m in r.methods:
                stripped = [c for c in r.calls if c not in r.guard_calls]
                mutated.append(RouteInfo(path=r.path, methods=r.methods, calls=stripped,
                                         endpoint=r.endpoint, kind=r.kind))
            else:
                mutated.append(r)
        _, problems = classify(mutated, policy)
        named = [x for x in problems if f"{m} {p}  [" in x]
        check(f"mutation: removing the guard from {m} {p} fails the gate", bool(named),
              f"problems={problems[:3]}")

    # Re-adding the deleted route with no guard, the way it was, fails by name.
    from fastapi import APIRouter, FastAPI
    from server.routes import agents as agents_routes

    async def dispatch():  # the shape of the deleted handler; never called
        return {}
    shadow = APIRouter(prefix="/api/agents")
    shadow.add_api_route("/dispatch", dispatch, methods=["POST"])
    tmp = FastAPI()
    tmp.include_router(shadow)
    tmp.include_router(agents_routes.router)
    extra, _ = effective_routes(tmp)
    _, problems = classify(routes + [x for x in extra if x.path == "/api/agents/dispatch"], policy)
    check("mutation: an unguarded POST /api/agents/dispatch fails the gate by name",
          any(x.startswith("UNGUARDED POST /api/agents/dispatch") for x in problems), str(problems[:3]))

    # A scoped WebSocket route cannot work (HTTP-only middleware and guard),
    # so the gate must fail it, listed or not.
    from server.applications.api_contract import SCOPE_ATTR

    async def _ws_guard():  # stands in for requires_permission(...)'s dependency
        return None
    setattr(_ws_guard, SCOPE_ATTR, "read:system")
    ws = RouteInfo(path="/api/probe/ws", methods=("WEBSOCKET",), calls=[_ws_guard],
                   endpoint="probe:ws", kind="APIWebSocketRoute")
    _, problems = classify(routes + [ws], policy)
    check("mutation: a scoped /api WebSocket route fails the gate by name",
          any("WEBSOCKET /api/probe/ws" in x and "WebSocket route relies on requires_permission" in x
              for x in problems), str(problems[:3]))

    from server.applications.route_policy import SHELL_ORIGIN_ATTR

    async def _ws_shell_guard():  # stands in for shell_origin_when_tokenless
        return None
    setattr(_ws_shell_guard, SHELL_ORIGIN_ATTR, True)
    ws2 = RouteInfo(path="/api/probe/ws2", methods=("WEBSOCKET",), calls=[_ws_shell_guard],
                    endpoint="probe:ws2", kind="APIWebSocketRoute")
    listed = dict(policy)
    listed[("WEBSOCKET", "/api/probe/ws2")] = ("shell-origin", "listed, as a mistaken author would")
    _, problems = classify(routes + [ws2], listed)
    check("mutation: a shell-origin /api WebSocket route fails the gate by name, even when listed",
          any("WEBSOCKET /api/probe/ws2" in x and "shell_origin_when_tokenless" in x
              for x in problems), str(problems[:3]))

    # Listing a route as shell-origin without the dependency is caught too.
    mutated = []
    for r in routes:
        if r.path == "/api/appearance/layout" and "PUT" in r.methods:
            mutated.append(RouteInfo(path=r.path, methods=r.methods,
                                     calls=[c for c in r.calls if c not in r.guard_calls],
                                     endpoint=r.endpoint, kind=r.kind))
        else:
            mutated.append(r)
    _, problems = classify(mutated, policy)
    check("mutation: a shell-origin entry whose route lost the dependency fails the gate",
          any("PUT /api/appearance/layout" in x and "no shell_origin_when_tokenless" in x for x in problems),
          str(problems[:3]))


# ── main ────────────────────────────────────────────────────────────────────

def main() -> int:
    from server.applications.route_policy import UNSCOPED_ROUTES

    app = load_app()
    routes, flatten_problems = effective_routes(app)
    api = [r for r in routes if r.path.startswith("/api/")]
    n_api = sum(len(r.methods) for r in api)

    print("the route table")
    check("the route table was read whole (no unexpanded router, no /api Mount)",
          not flatten_problems, "; ".join(flatten_problems))
    # A gate that enumerates nothing passes everything. 736 method+path pairs on
    # main when this gate was written, 733 once three were removed; a
    # count under 300 means the walk broke, not that the app shrank.
    check(f"the walk found the app's /api surface ({n_api} method+path pairs)", n_api >= 300)
    check("anchor routes present (system health, brain messages, a WebSocket)",
          _resolve(api, "GET", "/api/system/health") is not None
          and _resolve(api, "POST", "/api/brain/messages") is not None
          and any(r.methods == ("WEBSOCKET",) for r in api))

    rows, problems = classify(routes, UNSCOPED_ROUTES)
    counts: dict[str, int] = {}
    for cls in rows.values():
        counts[cls] = counts.get(cls, 0) + 1
    print("\n  /api routes by guard: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1])))
    if "--list" in sys.argv:
        for (m, p), cls in sorted(rows.items(), key=lambda kv: (kv[0][1], kv[0][0])):
            print(f"    {cls:<15} {m:<9} {p}")

    print("\nthe rule: a scope, or an entry in route_policy.py")
    for prob in problems:
        print("    " + prob)
    check(f"every /api route requires a permission or is listed with a reason "
          f"({len(problems)} problem(s))", not problems)

    print("\nremoved routes stay removed")
    for m, p in REMOVED:
        r = _resolve(api, m, p)
        check(f"{m} {p} reaches only the /api 404 catch-all",
              r is not None and r.endpoint.endswith(":_api_route_not_found"),
              f"resolves to {r.endpoint if r else None}")

    print("\nbehaviour (real middleware, real guards, stub endpoint)")
    behaviour(routes)

    print("\nmutations")
    mutations(routes, UNSCOPED_ROUTES)

    if failures:
        print(f"\n{len(failures)} FAILED")
        return 1
    print(f"\nPASS — {n_api} /api method+path pairs: every one scoped or listed with a reason")
    return 0


if __name__ == "__main__":
    try:
        rc = main()
    finally:
        _TMP.cleanup()
    sys.exit(rc)
