#!/usr/bin/env python3
"""Gate: every CEIGAS map key names a live route, every app route is under the
kill switch, and under the switch only the named stops get through.

WHY (2026-10-01). server/middleware/ceigas_gate.py checks a request only when
its path starts with a ROUTE_DOMAIN_MAP key. The launcher's key was "/api/apps";
routes/apps.py was retired on 2026-05-22 (33f6668c) and /api/applications took
over, but the key never moved, so the kill switch reached no app route. The only
test of the map asserted that "/api/apps" was a key, so it passed on the dead
key and could not fail.

The rule for the kill switch on app routes: with the switch on,
closing an app and stopping or disabling its process are still allowed; launch,
install and every settings change stay refused.

Since 2026-10-17 the gate checks only the kill switch (brain/ceigas_kill_switch.py);
the domain controller's grant engine is retired. What a caller may do is the app
token's scopes and, for the Host's own tools, the capability gate.

WHAT IT CHECKS, against server.main's BUILT route table and its real middleware
stack (never the source text):

  1. No dead keys. Every ROUTE_DOMAIN_MAP prefix matches at least one live /api
     route (the 404 catch-all does not count), and every bypass entry names a
     live route too. The stop shapes (ceigas_gate.STOP_ROUTES) match exactly
     the close and runtime routes.
  1c. Retired scopes (write:incident_decision, write:policy_proposals): the
     published Security 2.0.6 manifest still loads (scripts/fixtures), holding
     neither; no route requires one; validate still refuses an unknown scope;
     a token launched from an old registry row and the install-consent preview
     hold neither.
  2. Coverage. With the switch on and the stop exemption emptied, every
     /api/applications route, every method, is sent through server.main.app
     over httpx.ASGITransport (the transport kernel_op uses) and must be refused
     403 kill_switch_active before its handler runs. Only the app runtime pod's
     lane (GET /{app_id}/source, POST /{app_id}/runtime-log) passes. A mapped
     governance route (/api/system) passes the switch; a mapped memory route
     (/api/brain) does not.
  2b. The gate labels a CONCRETE path a stop by its shape, so every such path
     (an arbitrary id, and each static segment a live /api/applications route
     uses) is sent through the real router under the switch and must run
     close_window / app_runtime. A route registered above them cannot run as a
     "stop".
  3. Kill switch, the real exemption, through server.main.app: launch is
     refused; close, and runtime with op stop or disable, reach their handlers;
     runtime with op start, enable or status, or with a body the gate cannot be
     sure of, is refused; every other gated app write is refused. The exemption
     is named crossings only.
  3b. The Host's own stops under the switch: system.stop_app through the real
     meta-tool gate and close_app through the MCP lane's _ceigas_gate_synaptive
     pass; start/restart/open/uninstall and app_status, open_app and list_apps,
     an external MCP client's close, and the Host's inference crossing are
     refused. With the switch off, all of them pass the switch.
  4. Normal operation: the shell's launch, close and runtime stop pass the real
     CEIGASGateMiddleware and the handler still reads the body the gate read,
     with the switch off and (for the stops) on. With the switch off, no gated
     route under any mapped prefix is refused.
  5. Mutations, each of which must fail a check above by name: launch added
     to STOP_ROUTES; "write" added to the exemption; the exemption removed; the
     Host's crossing dropped from it; a POST /api/applications/{x}/{y} or
     /sites/close route registered above close; the map put back as it was on
     origin/main.

Container-free: imports server.main through scripts/test_api_route_guards.py's
load_app and reads routes with its effective_routes. The kill-switch file is
pointed at a temp dir, so the real /etc/privatae file is never read or written.
No DB (a fake pool serves the grant rows), no network, no pod.

Run: APP_ROOT=<repo> python3 scripts/test_ceigas_gate_routes.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import tempfile
from pathlib import Path

from fastapi import FastAPI, Request

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(ROOT / "scripts"))
# Puts the repo and brain/ on sys.path, points TM_JOBS_ROOT at a temp dir, and
# gives the one route-table walker both gates read.
import test_api_route_guards as _rt  # noqa: E402

_TMP = tempfile.TemporaryDirectory(prefix="ceigas-gate-routes-")
SCRATCH = Path(_TMP.name)

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(("  OK   " if ok else "  FAIL ") + label + (f"\n         {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(label)


def report(label: str, problems: list[str]) -> None:
    for p in problems:
        print("    " + p)
    check(f"{label} ({len(problems)} problem(s))", not problems)


ENTITY_ID = 9003
RELAY = "container.relay"
# The grants every live Host carried on 2026-10-01 (all eight identical).
LIVE_GRANTS = {
    "bots.execution": "execute", "container.relay": "read_write", "core.cognitive": "execute",
    "core.entity": "admin", "core.governance": "read", "core.memory": "admin",
    "device.filesystem": "read_write", "external.mcp": "read_write",
    "external.operator": "read_write", "infra.codebase": "admin", "synaptive.shape": "admin",
}
RUNTIME_POD_LANE = {
    ("GET", "/api/applications/{app_id}/source"),
    ("POST", "/api/applications/{app_id}/runtime-log"),
}
LAUNCH = ("POST", "/api/applications/{app_id}/launch")
CLOSE = ("POST", "/api/applications/{app_id}/close")
RUNTIME = ("POST", "/api/applications/{app_id}/runtime")
STOP_SHAPED = {CLOSE, RUNTIME}
STOP_ENDPOINTS = {"/close": "server.routes.applications:close_window",
                  "/runtime": "server.routes.applications:app_runtime"}
WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")
_UNSET = object()


class FakePool:
    """Answers the capability gate's grant read with what the live Hosts
    carry, and records every statement it executes."""

    def __init__(self):
        self.grants = dict(LIVE_GRANTS)
        self.executed: list[tuple[str, tuple]] = []

    async def fetch(self, sql, *args):
        if "SELECT domain_id, access_level FROM entity_domain_access" in sql:
            return ([{"domain_id": d, "access_level": lv} for d, lv in self.grants.items()]
                    if args[0] == ENTITY_ID else [])
        return []

    async def fetchrow(self, sql, *args):
        return None

    async def fetchval(self, sql, *args):
        return None

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        return "OK"


def _ks():
    import ceigas_kill_switch as ks
    return ks


def switch(on: bool) -> None:
    f = _ks().KILL_SWITCH_FILE
    if on:
        f.write_text("active")
    else:
        f.unlink(missing_ok=True)


def _concrete(template: str) -> str:
    return re.sub(r"\{[^}]+\}", "probe", template)


async def _request(app, method: str, path: str, body=_UNSET, raw: bytes | None = None):
    import httpx
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as c:
        kw: dict = {}
        if raw is not None:
            kw = {"content": raw, "headers": {"content-type": "application/json"}}
        elif method in ("POST", "PUT", "PATCH"):
            kw = {"json": {} if body is _UNSET else body}
        r = await c.request(method, path, **kw)
        try:
            out = r.json()
        except Exception:
            out = None
        return r.status_code, out if isinstance(out, dict) else {}


def _refused(status: int, out: dict) -> bool:
    return status == 403 and out.get("error") == "ceigas_denied" and out.get("reason") == "kill_switch_active"


# ── 1. no dead keys; the stop shapes are exactly close and runtime ──────────

def dead_keys(routes: list) -> list[str]:
    """Read against the WHOLE table: a bypass entry may name a non-/api route
    (BYPASS_PATHS holds the /ws socket)."""
    from server.middleware import ceigas_gate as g
    live = [r for r in routes if not r.endpoint.endswith(":_api_route_not_found")]
    problems = []
    for prefix in g.ROUTE_DOMAIN_MAP:
        if not any(r.path.startswith(prefix) for r in live):
            problems.append(f"ROUTE_DOMAIN_MAP key {prefix!r} matches no live route")
    for p in g.BYPASS_PATHS:
        if not any(r.path == p for r in live):
            problems.append(f"BYPASS_PATHS entry {p!r} is not a live route")
    for p in g.BYPASS_PREFIXES:
        if not any(r.path.startswith(p) for r in live):
            problems.append(f"BYPASS_PREFIXES entry {p!r} matches no live route")
    for pre, suf in g.BYPASS_SHAPES:
        if not any(r.path.startswith(pre) and r.path.endswith(suf) for r in live):
            problems.append(f"BYPASS_SHAPES entry {pre!r}..{suf!r} matches no live route")
    return problems


def stop_shapes(routes: list) -> list[str]:
    from server.middleware import ceigas_gate as g
    hit = set()
    for r in routes:
        for m in r.methods:
            for method, pre, suf, _ in g.STOP_ROUTES:
                if m == method and g._one_segment_between(r.path, pre, suf):
                    hit.add((m, r.path))
    problems = [f"{m} {p} has a stop shape but is not close or runtime" for m, p in sorted(hit - STOP_SHAPED)]
    problems += [f"{m} {p} is not matched by any stop shape" for m, p in sorted(STOP_SHAPED - hit)]
    return problems


def _gated(path: str) -> bool:
    from server.middleware import ceigas_gate as g
    p = _concrete(path)
    return (any(p.startswith(k) for k in g.ROUTE_DOMAIN_MAP)
            and not (p in g.BYPASS_PATHS or p.startswith(g.BYPASS_PREFIXES) or g._bypass_shape(p)))


# ── 1c. retired scopes ──────────────────────────────────────────────────────

def retired_scopes(routes: list) -> list[str]:
    """A scope removed from the catalogue stays accepted by the manifest
    loader (published manifests still declare it), is dropped from what the
    app holds, and is required by no route."""
    from server.applications import manifest as mf
    from server.applications import scopes as sc
    problems: list[str] = []
    retired = set(getattr(sc, "RETIRED_PERMISSIONS", ()))
    for name in ("write:incident_decision", "write:policy_proposals"):
        if name not in retired:
            problems.append(f"{name} is not in scopes.RETIRED_PERMISSIONS")
    if retired & set(sc.PERMISSIONS):
        problems.append(f"retired scopes still offered in PERMISSIONS: {sorted(retired & set(sc.PERMISSIONS))}")
    try:
        m = mf.load(ROOT / "scripts" / "fixtures" / "security_app_2.0.6.yaml")
        if m.version != "2.0.6":
            problems.append(f"the fixture is not Security 2.0.6 ({m.version})")
        if retired & set(m.permissions):
            problems.append(f"Security 2.0.6 loaded holding retired scopes {sorted(retired & set(m.permissions))}")
    except Exception as e:
        problems.append(f"Security 2.0.6 (published with the retired scopes) no longer loads: {e}")
    for r in routes:
        for scope in r.scopes:
            if scope in retired:
                problems.append(f"{sorted(r.methods)} {r.path} requires retired scope {scope}")
    try:
        sc.validate(["write:no-such-scope"])
        problems.append("an unknown scope is accepted (the retired set must not widen validate)")
    except ValueError:
        pass
    return problems


async def retired_scopes_at_doors(app) -> list[str]:
    """The two doors past the manifest loader: a token minted at launch from a
    registry row an older kernel wrote (it still holds the retired scopes),
    through server.main.app's real launch route; and the install-consent
    preview of a bundle whose manifest declares them."""
    import types
    from datetime import datetime, timezone
    import server.routes.applications as apps_mod
    from server import brain_service
    from server.applications import registry as reg
    from server.applications import tokens as tk
    from server.applications.scopes import RETIRED_PERMISSIONS
    problems: list[str] = []
    old_yaml = (ROOT / "scripts" / "fixtures" / "security_app_2.0.6.yaml").read_text()
    import yaml as _yaml
    old_perms = list(_yaml.safe_load(old_yaml)["permissions"])
    if not set(RETIRED_PERMISSIONS) <= set(old_perms):
        problems.append("the fixture no longer declares the retired scopes; the test means nothing")

    src = SCRATCH / "security-2.0.6"
    src.mkdir(exist_ok=True)
    (src / "app.yaml").write_text(old_yaml)
    row = reg.InstalledApp(
        id="security", name="Security", version="2.0.6", provenance="standard",
        entrypoint="index.html", permissions=old_perms, source_dir=str(src),
        source_signature=None, installed_at=datetime.now(timezone.utc), upgraded_at=None,
        status="active", app_license={}, authored_here=None)

    async def fake_get(pool, app_id):
        return row if app_id == "security" else None

    secret = b"retired-scope-test-secret-32byte"
    saved = (brain_service.get_pool, reg.get, getattr(app.state, "kernel_secret", None))
    brain_service.get_pool = lambda: object()
    reg.get = fake_get
    app.state.kernel_secret = secret
    try:
        status, out = await _request(app, "POST", "/api/applications/security/launch", body={})
        tok = out.get("token")
        held = set(tk.verify(secret, tok).permissions) if tok else None
        if status != 200 or held is None:
            problems.append(f"launch of the old row did not mint a token: {status} {out}")
        elif held & set(RETIRED_PERMISSIONS) or set(out.get("permissions") or []) & set(RETIRED_PERMISSIONS):
            problems.append(f"the token minted from an old registry row holds {sorted(held & set(RETIRED_PERMISSIONS))}")
        elif "read:incidents" not in held:
            problems.append(f"the token lost a live scope: {sorted(held)}")
    finally:
        brain_service.get_pool, reg.get = saved[0], saved[1]
        app.state.kernel_secret = saved[2]

    async def fake_fetch(app_id, version):
        return b"bundle"
    saved_fetch, saved_read = apps_mod._fetch_bundle_from_store, apps_mod.bundles._read_zip
    apps_mod._fetch_bundle_from_store = fake_fetch
    apps_mod.bundles._read_zip = lambda data: types.SimpleNamespace(manifest_yaml=old_yaml)
    try:
        pv = await apps_mod.store_preview("security")
        shown = set(pv.get("permissions") or [])
        if shown & set(RETIRED_PERMISSIONS):
            problems.append(f"the install-consent preview lists {sorted(shown & set(RETIRED_PERMISSIONS))}")
        if "read:incidents" not in shown:
            problems.append(f"the preview lost a live scope: {sorted(shown)}")
    finally:
        apps_mod._fetch_bundle_from_store, apps_mod.bundles._read_zip = saved_fetch, saved_read
    return problems


# ── 2. every /api/applications route is under the switch ────────────────────

async def coverage(app, api: list) -> tuple[list[str], set, set]:
    """Switch ON with the stop exemption EMPTIED: a request the gate covers is
    refused, so a request that reaches its handler is one the gate missed."""
    ks = _ks()
    pairs = sorted({(m, r.path) for r in api if r.path.startswith("/api/applications")
                    for m in r.methods if m != "WEBSOCKET"})
    problems, gated, passed = [], set(), set()
    saved = ks.KILL_SWITCH_STOP_CROSSINGS
    ks.KILL_SWITCH_STOP_CROSSINGS = frozenset()
    switch(True)
    try:
        for m, tmpl in pairs:
            status, out = await _request(app, m, _concrete(tmpl))
            (gated if _refused(status, out) else passed).add((m, tmpl))
    finally:
        switch(False)
        ks.KILL_SWITCH_STOP_CROSSINGS = saved
    for m, tmpl in pairs:
        if (m, tmpl) in passed and (m, tmpl) not in RUNTIME_POD_LANE:
            kind = "write" if m in WRITE_METHODS else "read"
            problems.append(f"UNGATED {kind} {m} {tmpl}: reached its handler with the switch on")
    for m, tmpl in sorted(RUNTIME_POD_LANE):
        if (m, tmpl) not in pairs:
            problems.append(f"the runtime pod lane route {m} {tmpl} is gone — update RUNTIME_POD_LANE "
                            f"and ceigas_gate.BYPASS_SHAPES together")
        elif (m, tmpl) in gated:
            problems.append(f"{m} {tmpl} is refused by the switch; the runtime pod lane expects it bypassed")
    return problems, gated, passed


# ── 2b. a stop-shaped concrete path runs close_window / app_runtime ────────

async def _dispatched_endpoint(app, method: str, path: str, body) -> str | None:
    """The endpoint the REAL router ran for this request, in dispatch order:
    read off the scope the router filled in, after the whole stack ran."""
    seen: dict = {}

    async def outer(scope, receive, send):
        await app(scope, receive, send)
        ep = scope.get("endpoint")
        seen["ep"] = f"{getattr(ep, '__module__', '?')}:{getattr(ep, '__qualname__', '?')}" if ep else None

    await _request(outer, method, path, body=body)
    return seen.get("ep")


async def stop_dispatch_problems(app, routes: list) -> list[str]:
    """Switch ON. The gate lets a CONCRETE path through as a stop by its
    shape; every such path must run the stop's own handler, whatever route
    might sit above it: an arbitrary id, and every static segment a live
    /api/applications route uses (the names a later route is most likely to
    take)."""
    from server.middleware import ceigas_gate as g
    segs = {"probe"} | {r.path.split("/")[3] for r in routes
                        if r.path.startswith("/api/applications/") and r.path.count("/") >= 3
                        and not r.path.split("/")[3].startswith("{")}
    problems = []
    switch(True)
    try:
        for method, pre, suf, _ in g.STOP_ROUTES:
            body = {"window_id": "probe"} if suf == "/close" else {"op": "stop"}
            for seg in sorted(segs):
                path = f"{pre}{seg}{suf}"
                ep = await _dispatched_endpoint(app, method, path, body)
                if ep != STOP_ENDPOINTS.get(suf):
                    problems.append(f"{method} {path} is let through as a stop but runs {ep}")
    finally:
        switch(False)
    return problems


# ── 3. under the kill switch, only the named stops pass ─────────────────────

async def kill_switch_problems(app, gated: set, full: bool = True) -> list[str]:
    """Run with the switch ON. full=False (the mutations) asks only about
    launch and close."""
    ks = _ks()
    problems: list[str] = []

    async def refused(label, method, path, body=_UNSET, raw=None):
        status, out = await _request(app, method, path, body=body, raw=raw)
        if not _refused(status, out):
            problems.append(f"{label}: should be refused under the kill switch — got {status} {out}")

    async def stopped(label, path, body, handler_status, handler_says=None):
        status, out = await _request(app, "POST", path, body=body)
        if not (status == handler_status and out.get("error") != "ceigas_denied"
                and (handler_says is None or handler_says(out))):
            problems.append(f"{label}: should pass under the kill switch to its handler — got {status} {out}")

    if not ks.is_kill_switch_active():
        return ["the kill switch is not on — nothing below means anything"]

    launch = _concrete(LAUNCH[1])
    close = _concrete(CLOSE[1])
    runtime = _concrete(RUNTIME[1])

    await refused("launch", "POST", launch)
    # close reaches its handler, which answers for a window it was told about.
    await stopped("close", close, {"window_id": "probe-window"}, 200,
                  lambda o: o.get("closed") == "probe-window")
    if not full:
        return problems
    # runtime stop/disable reach the handler, which parses a VALID op from the
    # body the gate already read and then stops at the pool: 503, not 400/500.
    for op in ("stop", "disable", " Stop "):
        await stopped(f"runtime op={op!r}", runtime, {"op": op}, 503,
                      lambda o: o.get("detail") == "brain pool down")
    for op in ("start", "enable", "status"):
        await refused(f"runtime op={op!r}", "POST", runtime, body={"op": op})
    await refused("runtime with an empty object (op status)", "POST", runtime, body={})
    await refused("runtime with no body", "POST", runtime, raw=b"")
    await refused("runtime with a list body", "POST", runtime, raw=b'["stop"]')
    await refused("runtime whose duplicate op key ends on start", "POST", runtime,
                  raw=b'{"op": "stop", "op": "start"}')
    await refused("runtime with a body past the gate's read cap", "POST", runtime,
                  raw=json.dumps({"op": "stop", "pad": "x" * 5000}).encode())
    await refused("runtime with a body that is not JSON", "POST", runtime, raw=b"op=stop")

    for m, tmpl in sorted(gated):
        if m not in WRITE_METHODS or (m, tmpl) in STOP_SHAPED or (m, tmpl) == LAUNCH:
            continue
        await refused(f"{m} {tmpl}", m, _concrete(tmpl))

    # The exemption is one crossing per lane, not an operation name or a domain pair.
    for src, tgt, op in ((RELAY, "device.filesystem", "stop"), (RELAY, RELAY, "write"),
                         (RELAY, RELAY, "delete"), ("external.mcp", "core.entity", "stop")):
        if not ks.kill_switch_refuses(src, tgt, op):
            problems.append(f"{src} -> {tgt} {op!r} passed the kill switch")
    return problems


# ── 3b. the Host's own close and stop under the kill switch ─────────────────

async def host_stop_problems(pool: FakePool, switch_on: bool = True) -> list[str]:
    """The Host's lanes: the meta-tool gate (system.stop_app; the scheduled op
    lane goes through it too), the in-pod MCP surface (close_app through
    _ceigas_gate_synaptive) and the inference crossing."""
    from server import brain_service
    from server.routes.mcp import _TOOL_CEIGAS_MAP, _ceigas_gate_synaptive
    from mastercode.runtime.tools import _authorize_meta_tool
    ks = _ks()
    problems: list[str] = []

    ok, env = await _authorize_meta_tool("system.stop_app", {"app_id": "probe"}, ENTITY_ID, pool, None)
    if not ok:
        problems.append(f"meta-tool system.stop_app: should pass the switch — got {ok} {env}")
    for name in ("system.start_app", "system.restart_app", "open_app", "uninstall_app",
                 "system.app_status"):
        ok, env = await _authorize_meta_tool(name, {"app_id": "probe"}, ENTITY_ID, pool, None)
        said = (env or {}).get("result")
        if switch_on and (ok or said != "kill switch active"):
            problems.append(f"meta-tool {name}: should be refused by the switch — got {ok} {env}")
        if not switch_on and said == "kill switch active":
            problems.append(f"meta-tool {name}: refused by a switch that is off — {env}")

    saved_get_pool = brain_service.get_pool
    brain_service.get_pool = lambda: pool
    try:
        for tool in ("close_app", "open_app", "list_apps"):
            target, op, bits = _TOOL_CEIGAS_MAP[tool]
            res = await _ceigas_gate_synaptive(
                entity_id=ENTITY_ID, tool_name=tool, source_domain="core.entity",
                target_domain=target, operation=op, required_bits=bits, token_scopes=None,
                is_internal=True, metadata={"tool": tool})
            if tool == "close_app" or not switch_on:
                if not res.get("allowed"):
                    problems.append(f"MCP {tool} (the Host, core.entity): should pass — got {res}")
            elif res.get("allowed") or res.get("reason") != "kill_switch_active":
                problems.append(f"MCP {tool}: should be refused by the switch — got {res}")
    finally:
        brain_service.get_pool = saved_get_pool

    if ks.kill_switch_refuses("core.entity", "core.cognitive", "execute") != switch_on:
        problems.append(f"the Host's inference crossing: refused={not switch_on} expected {switch_on}")
    return problems


async def amain() -> int:
    for name in ("ceigas.gate", "privatae.ceigas.kill_switch",
                 "privatae.applications.middleware", "privatae.pc.app_middleware"):
        logging.getLogger(name).setLevel(logging.CRITICAL)

    app = _rt.load_app()
    routes, flat_problems = _rt.effective_routes(app)
    api = [r for r in routes if r.path.startswith("/api/")]
    n_api = sum(len(r.methods) for r in api)

    ks = _ks()
    from server.middleware import ceigas_gate as g
    from server.middleware.ceigas_gate import CEIGASGateMiddleware
    real_file = ks.KILL_SWITCH_FILE
    ks.KILL_SWITCH_FILE = SCRATCH / "ceigas_kill_switch"

    print("the route table")
    check("the route table was read whole", not flat_problems, "; ".join(flat_problems))
    check(f"the walk found the app's /api surface ({n_api} method+path pairs)", n_api >= 300)
    app_pairs = {(m, r.path) for r in api if r.path.startswith("/api/applications") for m in r.methods}
    n_writes = sum(1 for m, _ in app_pairs if m in WRITE_METHODS)
    check(f"/api/applications found ({len(app_pairs)} pairs, {n_writes} writes)",
          len(app_pairs) >= 50 and n_writes >= 30)
    check("the kill-switch file is the temp one, and off (never /etc/privatae)",
          ks.KILL_SWITCH_FILE != real_file and not ks.is_kill_switch_active())

    print("\n1. every map key and bypass entry names a live route; stops are close and runtime")
    report("no dead keys", dead_keys(routes))
    report("the stop shapes match exactly POST close and POST runtime", stop_shapes(routes))
    report("retired scopes: Security 2.0.6 still loads, holding none of them; no route requires "
           "one; validate still refuses an unknown scope", retired_scopes(routes))
    # Removed 2026-10-01 with the incident lane (nothing fed it). Each must
    # reach only the /api 404 catch-all.
    for m, path in (("GET", "/api/ceigas/incidents"), ("POST", "/api/ceigas/incidents/x/grant"),
                    ("POST", "/api/ceigas/incidents/x/deny"), ("POST", "/api/ceigas/policy/revoke"),
                    ("POST", "/api/portal/sources/folder")):
        r = _rt._resolve(api, m, path)
        check(f"{m} {path} stays removed (only the /api 404 catch-all answers)",
              r is not None and r.endpoint.endswith(":_api_route_not_found"),
              f"resolves to {r.endpoint if r else None}")

    found = False
    for m in app.user_middleware:
        if m.cls is CEIGASGateMiddleware:
            m.kwargs["entity_id"] = ENTITY_ID
            found = True
    check("CEIGASGateMiddleware is in server.main's middleware stack", found)
    check("the gate takes no controller (the switch is its only input)",
          all("domain_controller" not in m.kwargs for m in app.user_middleware
              if m.cls is CEIGASGateMiddleware))

    print("\n2. every /api/applications route is under the switch (server.main.app)")
    switch(True)
    try:
        s1, o1 = await _request(app, "GET", "/api/brain/memory")
        s2, o2 = await _request(app, "GET", "/api/system/resources")
    finally:
        switch(False)
    check("control: a mapped memory route (/api/brain) is refused under the switch", _refused(s1, o1), f"{s1} {o1}")
    check("control: a mapped governance route (/api/system) passes the switch",
          not _refused(s2, o2), f"{s2} {o2}")
    probs, gated, passed = await coverage(app, api)
    gated_writes = sum(1 for m, _ in gated if m in WRITE_METHODS)
    report(f"every app route is refused with the exemption emptied, but the runtime pod lane "
           f"({len(gated)} gated, {gated_writes} of them writes)", probs)
    check("exactly the runtime pod lane passes ungated", passed == RUNTIME_POD_LANE, str(sorted(passed)))

    print("\n2b. a stop-shaped path runs the stop's own handler (real router, dispatch order)")
    report("every stop-shaped concrete path runs close_window / app_runtime",
           await stop_dispatch_problems(app, routes))
    report("retired scopes at the other doors: a token launched from an old registry row and the "
           "install-consent preview hold neither", await retired_scopes_at_doors(app))

    print("\n3. kill switch, the real exemption, through server.main.app")
    switch(True)
    try:
        check("kill switch on", ks.is_kill_switch_active())
        report("launch and every other app write refused; close and runtime stop/disable pass",
               await kill_switch_problems(app, gated))
        print("\n3b. the Host's own close and stop under the kill switch")
        report("system.stop_app and the Host's close_app pass; every other Host app tool, reads "
               "included, an external client's close and the inference crossing are refused",
               await host_stop_problems(FakePool()))
    finally:
        switch(False)
    report("switch off: the Host's app tools, its MCP app tools and its inference all pass the switch",
           await host_stop_problems(FakePool(), switch_on=False))

    print("\n4. normal operation through the real gate")

    def probe_app(paths=None):
        probe = FastAPI()

        async def _stub(request: Request):
            raw = await request.body()
            return {"reached": True, "body": json.loads(raw) if raw else None}
        for m, tmpl in (paths or (LAUNCH, CLOSE, RUNTIME)):
            probe.add_api_route(tmpl, _stub, methods=[m])
        probe.add_middleware(CEIGASGateMiddleware, entity_id=ENTITY_ID)
        return probe

    probe = probe_app()
    for (m, tmpl), body, on in ((LAUNCH, {}, False), (CLOSE, {"window_id": "w"}, False),
                                (RUNTIME, {"op": "stop"}, False), (RUNTIME, {"op": "start"}, False),
                                (CLOSE, {"window_id": "w"}, True), (RUNTIME, {"op": "stop"}, True)):
        switch(on)
        try:
            status, out = await _request(probe, m, _concrete(tmpl), body=body)
        finally:
            switch(False)
        check(f"switch {'on' if on else 'off'}: {tmpl.rsplit('/', 1)[-1]} {body} passes and the "
              f"handler reads the same body",
              status == 200 and out.get("reached") is True and out.get("body") == body, f"{status} {out}")

    pairs = sorted({(m, r.path) for r in routes if r.path.startswith("/api/")
                    and not r.endpoint.endswith(":_api_route_not_found") and _gated(r.path)
                    for m in r.methods if m != "WEBSOCKET"})
    probe = probe_app(pairs)
    refused = []
    for m, tmpl in pairs:
        status, out = await _request(probe, m, _concrete(tmpl))
        if status == 403 and out.get("error") == "ceigas_denied":
            refused.append((m, tmpl))
    check(f"switch off: none of the {len(pairs)} gated method+path pairs is refused",
          not refused and len(pairs) >= 100, str(refused[:5]))

    print("\n5. mutations, each named by the check that must catch it")
    saved_stops = g.STOP_ROUTES
    saved_exempt = ks.KILL_SWITCH_STOP_CROSSINGS
    try:
        switch(True)
        g.STOP_ROUTES = saved_stops + (("POST", "/api/applications/", "/launch", False),)
        probs = await kill_switch_problems(app, gated, full=False)
        check("launch added to STOP_ROUTES: check 3 names launch",
              any(p.startswith("launch:") for p in probs), str(probs[:3]))
        check("... and so does the stop-shape check",
              any("/launch has a stop shape" in p for p in stop_shapes(routes)))
        g.STOP_ROUTES = saved_stops

        ks.KILL_SWITCH_STOP_CROSSINGS = saved_exempt | {(RELAY, RELAY, "write")}
        probs = await kill_switch_problems(app, gated, full=False)
        check("'write' added to the kill-switch exemption: check 3 names launch",
              any(p.startswith("launch:") for p in probs), str(probs[:3]))

        ks.KILL_SWITCH_STOP_CROSSINGS = frozenset()
        probs = await kill_switch_problems(app, gated, full=False)
        check("the exemption removed: check 3 names close (the allow side is checked too)",
              any(p.startswith("close:") for p in probs), str(probs[:3]))

        ks.KILL_SWITCH_STOP_CROSSINGS = frozenset({(RELAY, RELAY, "stop")})
        probs = await host_stop_problems(FakePool())
        check("the Host's crossing dropped from the exemption: check 3b names system.stop_app "
              "and close_app", any("system.stop_app" in p for p in probs)
              and any("MCP close_app" in p for p in probs), str(probs[:3]))
    finally:
        g.STOP_ROUTES = saved_stops
        ks.KILL_SWITCH_STOP_CROSSINGS = saved_exempt
        switch(False)

    from fastapi.routing import APIRoute

    async def _hijack():
        return {"hijacked": True}
    for tmpl, named in (("/api/applications/{x}/{y}", "/api/applications/probe/close"),
                        ("/api/applications/sites/close", "/api/applications/sites/close")):
        route = APIRoute(tmpl, _hijack, methods=["POST"])
        app.router.routes.insert(0, route)          # registered ABOVE close_window
        try:
            probs = await stop_dispatch_problems(app, routes)
        finally:
            app.router.routes.remove(route)
        check(f"a POST {tmpl} route registered above close: check 2b names {named}",
              any(p.startswith(f"POST {named} is let through as a stop but runs") and "_hijack" in p
                  for p in probs), str(probs[:3]))

    saved = dict(g.ROUTE_DOMAIN_MAP)
    try:
        g.ROUTE_DOMAIN_MAP.pop("/api/applications", None)
        g.ROUTE_DOMAIN_MAP["/api/apps"] = (RELAY, RELAY, None)
        probs = dead_keys(routes)
        check("origin/main's map: the dead '/api/apps' key fails check 1 by name",
              any("'/api/apps' matches no live route" in p for p in probs), str(probs))
        probs, _, _ = await coverage(app, api)
        named = [t for _, t in (LAUNCH, CLOSE, RUNTIME)
                 if any(f"UNGATED write POST {t}:" in p for p in probs)]
        check("... and launch, close and runtime fail check 2 by name", len(named) == 3, str(probs[:4]))
    finally:
        g.ROUTE_DOMAIN_MAP.clear()
        g.ROUTE_DOMAIN_MAP.update(saved)
        ks.KILL_SWITCH_FILE = real_file

    if failures:
        print(f"\n{len(failures)} FAILED")
        return 1
    print(f"\nPASS — every map key is live; {len(gated)} /api/applications routes are under the "
          f"kill switch; only the runtime pod lane ({len(RUNTIME_POD_LANE)}) bypasses it; under the "
          f"switch only close and runtime stop/disable get through")
    return 0


if __name__ == "__main__":
    try:
        rc = asyncio.run(amain())
    finally:
        _TMP.cleanup()
    sys.exit(rc)
