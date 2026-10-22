#!/usr/bin/env python3
"""The owner names the Host, and every place that shows the name shows it.

POST /api/system/name (server/routes/host_name.py) writes the name into the
Host's own entity_registry row. Pinned here:

  1. the route takes write:host_name and the owner gate, in that order;
  2. the owner gate refuses any app but Settings, Getting Started included,
     and refuses Settings without a live owner sign-in;
  3. every app the gate admits is served from its own origin: any page on
     the desktop's origin can get a same-origin app's token, so admitting
     one would let a Host-built app rename the Host. The network membership
     routes still admit Settings alone; Settings' Git server routes take the
     same gate, and every app it admits is on its own origin too;
  4. a name is trimmed, 1 to 40 characters, with no control or format
     characters, and anything else is refused with 400;
  5. a good name is written to this Host's row, and nothing is written for a
     bad one;
  6. the readers prefer the row's chosen name: /api/system/status and the
     presence line's host_name() return it, and fall back when the row holds
     a placeholder;
  7. write:host_name is a known scope, and no capability bit maps to it.

Container-free: APP_ROOT=<repo> python3 scripts/test_host_name_route.py
Fake pool and request objects; no database, no server.
"""
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1]).resolve()
sys.path[:0] = [str(ROOT), str(ROOT / "brain")]

FAILS: list[str] = []


def check(cond, msg, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + msg + (f"\n        {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(msg)


class FakePool:
    """Answers the two statements these routes run against entity_registry."""

    def __init__(self, name):
        self.name = name
        self.writes = []

    async def execute(self, sql, *args):
        self.writes.append((sql, args))
        if "UPDATE entity_registry SET display_name" in sql:
            self.name = args[0]
            return "UPDATE 1"
        return "UPDATE 0"

    async def fetchval(self, sql, *args):
        return self.name

    async def fetchrow(self, sql, *args):
        return {"display_name": self.name}


def request(app_id=None):
    from starlette.requests import Request
    req = Request({"type": "http", "method": "POST", "path": "/api/system/name",
                   "headers": [(b"host", b"home.cseries.localhost")], "state": {}})
    req.state.app_token = type("T", (), {"app_id": app_id})() if app_id else None
    return req


async def status_of(coro):
    from fastapi import HTTPException
    try:
        await coro
    except HTTPException as e:
        return e.status_code
    return 200


async def main():
    from fastapi.routing import APIRoute
    from server import brain_service
    from server.routes import host_name as routes
    from server.routes import network_membership

    # 1
    deps = {r.path: [getattr(d.call, "_app_scope", d.call.__name__) for d in r.dependant.dependencies]
            for r in routes.router.routes if isinstance(r, APIRoute)}
    check(deps.get("/api/system/name") == ["write:host_name", "owner_only"],
          "POST /api/system/name takes write:host_name, then the owner gate", str(deps))

    # 2
    for app_id in (None, "host", "network", "designs", "getting-started"):
        check(await status_of(routes.owner_only(request(app_id))) == 403,
              f"owner gate refuses the {app_id or 'token-less'} caller")
    check(await status_of(routes.owner_only(request("settings"))) == 403,
          "settings' token without the owner's sign-in is refused")

    async def signed_in(scope):
        return "owner@example.com", "tok"

    real_session = routes.live_session
    routes.live_session = signed_in
    try:
        check(await status_of(routes.owner_only(request("settings"))) == 200,
              "settings with the owner signed in may rename the Host")
        for app_id in ("host", "getting-started"):
            check(await status_of(routes.owner_only(request(app_id))) == 403,
                  f"a signed-in owner does not let {app_id}'s token rename the Host")
    finally:
        routes.live_session = real_session

    # 3 — a same-origin app's token can be minted by any page on the desktop's
    # origin (routes/applications.launch_app hands it over), and the owner's
    # cookie rides along, so only apps on their own origin may be admitted.
    import re
    from server.applications.proxy import is_isolated
    for app_id in routes.NAME_APP_IDS:
        m = re.search(r"(?m)^provenance:\s*(\S+)",
                      (ROOT / "system-apps" / app_id / "app.yaml").read_text())
        prov = m.group(1) if m else ""
        check(is_isolated(app_id, prov),
              f"{app_id}, admitted by the owner gate, is served from its own origin",
              f"provenance={prov!r}")
    check(network_membership.OWNER_APP_IDS == ("settings",),
          "network membership still admits Settings alone", str(network_membership.OWNER_APP_IDS))

    # 3b — Settings' Git server routes use the same gate: connecting decides
    # which server and account the Host works as.
    from server.routes import git_server as gs_routes
    gs_deps = {(sorted(r.methods)[0], r.path): [getattr(d.call, "_app_scope", d.call.__name__)
                                                for d in r.dependant.dependencies]
               for r in gs_routes.router.routes if isinstance(r, APIRoute)}
    check(gs_deps == {("GET", "/api/git-server"): ["read:git_server"],
                      ("POST", "/api/git-server"): ["write:git_server", "owner_only"],
                      ("DELETE", "/api/git-server"): ["write:git_server", "owner_only"]},
          "connect and disconnect take write:git_server, then the owner gate", str(gs_deps))
    for app_id in (None, "host", "network", "getting-started"):
        check(await status_of(gs_routes.owner_only(request(app_id))) == 403,
              f"git server owner gate refuses the {app_id or 'token-less'} caller")
    check(await status_of(gs_routes.owner_only(request("settings"))) == 403,
          "git server: settings' token without the owner's sign-in is refused")
    real_gs_session = gs_routes.live_session
    gs_routes.live_session = signed_in
    try:
        check(await status_of(gs_routes.owner_only(request("settings"))) == 200,
              "git server: settings with the owner signed in may connect")
        # Signed in, so a refusal here can only come from the app check.
        for app_id in (None, "host", "network", "getting-started"):
            check(await status_of(gs_routes.owner_only(request(app_id))) == 403,
                  f"git server: a signed-in owner does not let the {app_id or 'token-less'} caller connect")
    finally:
        gs_routes.live_session = real_gs_session
    for app_id in gs_routes.OWNER_APP_IDS:
        m = re.search(r"(?m)^provenance:\s*(\S+)",
                      (ROOT / "system-apps" / app_id / "app.yaml").read_text())
        prov = m.group(1) if m else ""
        check(is_isolated(app_id, prov),
              f"{app_id}, admitted by the git server owner gate, is served from its own origin",
              f"provenance={prov!r}")

    # 4
    # "\ud800": a lone surrogate, which a JSON body can carry and UTF-8 cannot store.
    bad = ["", "   ", "x" * 41, "two\nlines", "tab\there", "zero​width", "bell\x07", "lone\ud800half"]
    for raw in bad:
        check(await status_of(_clean(routes, raw)) == 400, f"refused: {raw!r}")
    check(routes.clean_name("  Ada  ") == "Ada", "a name is trimmed")
    check(routes.clean_name("x" * 40) == "x" * 40, "40 characters is allowed")
    check(routes.clean_name("Zoë's Host") == "Zoë's Host", "letters beyond ASCII and punctuation are allowed")

    # 5
    pool = FakePool("C-Series")
    real_pool, real_eid = brain_service.get_pool, brain_service._entity_id
    brain_service.get_pool = lambda: pool
    brain_service._entity_id = 9001
    try:
        out = await routes.set_name(routes.NameBody(name="  Juniper "))
        check(out == {"ok": True, "name": "Juniper"}, "the saved name is answered back", str(out))
        check(pool.writes and pool.writes[-1][1] == ("Juniper", 9001),
              "the name is written to this Host's own row", str(pool.writes))
        n = len(pool.writes)
        check(await status_of(routes.set_name(routes.NameBody(name="a\nb"))) == 400
              and len(pool.writes) == n, "a refused name writes nothing")

        # 6 — with a provisioned relay.json naming the Host something else,
        # which a rename never touches.
        import json
        import tempfile
        from server.routes import entity, system
        relay = Path(tempfile.mkdtemp()) / "relay.json"
        relay.write_text(json.dumps({"display_name": "Provisioned", "entity_display_name": "Provisioned"}))
        real_path, real_cfg = system.Path, entity.RELAY_CONFIG
        system.Path = lambda p: relay if str(p).endswith("relay.json") else Path(p)
        entity.RELAY_CONFIG = relay
        try:
            check(await entity.host_name() == "Juniper",
                  "the presence line's name is the row's name, not relay.json's")
            st = await system.status()
            check(st["entity_display_name"] == "Juniper",
                  "/api/system/status shows the row's name, not relay.json's", str(st))
            pool.name = "Host"
            st = await system.status()
            check(st["entity_display_name"] == "Provisioned",
                  "a placeholder row falls back to relay.json", str(st))
            pool.name = "Juniper"
        finally:
            system.Path, entity.RELAY_CONFIG = real_path, real_cfg
        pool.name = "Entity 9001"
        check(await entity.host_name() == entity._entity_name(),
              "a placeholder in the row falls back to the provisioned name or Host #N")
        st = await system.status()
        check(st["entity_display_name"] != "Entity 9001",
              "/api/system/status falls back past a placeholder", str(st))
    finally:
        brain_service.get_pool, brain_service._entity_id = real_pool, real_eid

    # 7
    from server.applications import scopes
    check(scopes.validate(["write:host_name"]) == ["write:host_name"], "write:host_name is a known scope")
    caps = "\n".join(p.read_text() for p in (ROOT / "server").rglob("*.py")
                     if "meta_tool_caps" in p.read_text() and p.name != "scopes.py")
    check("write:host_name" not in caps, "no capability bit maps to write:host_name")


async def _clean(routes, raw):
    routes.clean_name(raw)


asyncio.run(main())
if FAILS:
    print(f"\nFAIL: {len(FAILS)} check(s)")
    sys.exit(1)
print("\nall checks passed")
