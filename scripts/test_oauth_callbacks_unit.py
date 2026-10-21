#!/usr/bin/env python3
"""The OAuth returns on a Host the internet reaches, through the Host's own
gates (server/middleware/localhost_only.is_oauth_callback).

A provider sends the browser back to the Host with a code and the state the
Host sent. On an https Host the session cookie is SameSite=Strict, which a
browser leaves off a navigation another site started, so the return arrives
with no session, from a peer that is not loopback (the proxy in front). Each
return is let through for GET only because its handler acts on nothing but a
state this Host made, kept on this Host, unguessable and good once. Pinned:

  1. Without a session, from the proxy, GET reaches each return's own
     handler: a data provider's (/api/integrations/callback/<key>), gitea's
     and an MCP server's. Not a redirect to /login, not a 403.
  2. Anything else is not let through: POST, a path that is not a provider
     key, a provider with no manifest (404 before any module is looked up).
  3. gitea's return trades a code only for a PKCE entry this Host made: once,
     never past ten minutes, never for a state it did not make.
  4. A data provider's return refuses a state this Host signed but did not
     mint for a sign-in (scripts/test_google_connector_state.py pins the
     single use itself).

No database, no network: the code exchanges are stand-ins.

    python3 scripts/test_oauth_callbacks_unit.py      (from the repo root)
"""
import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ["CSERIES_URL"] = "https://home.box.example"
os.environ["CSERIES_TRUSTED_PROXIES"] = "10.88.0.1/32"

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

import httpx  # noqa: E402
from fastapi import FastAPI  # noqa: E402

from server import owner_login as L  # noqa: E402
from server.applications.middleware import ApplicationTokenMiddleware  # noqa: E402
from server.integrations.core import manifests, oauth  # noqa: E402
from server.middleware import localhost_only as G  # noqa: E402
from server.middleware.app_origin_guard import AppOriginGuard  # noqa: E402
from server.middleware.host_header import HostHeaderGuard  # noqa: E402
from server.middleware.owner_session import OwnerSessionGate  # noqa: E402
from server.routes import gitea as GT  # noqa: E402
from server.routes import integrations as I  # noqa: E402
from server.routes import mcp_oauth as M  # noqa: E402

failed = 0
HOST = "home.box.example"
PROXY = ("10.88.0.1", 41000)


def check(cond, what, detail=""):
    global failed
    if cond:
        print(f"OK    {what}")
    else:
        failed = 1
        print(f"FAIL  {what} {detail}")


def build():
    app = FastAPI()
    for r in (I.router, GT.router, M.router):
        app.include_router(r)
    # main.py's order, innermost first (CEIGAS passes everything while its
    # switch is off, and is left out).
    app.add_middleware(ApplicationTokenMiddleware)
    app.add_middleware(G.LocalhostOnlyMiddleware)
    app.add_middleware(G.LocalhostOnlyWebSocketGate)
    app.add_middleware(OwnerSessionGate)
    app.add_middleware(AppOriginGuard)
    app.add_middleware(HostHeaderGuard)
    return app


# What a browser sends on a provider's redirect back: a top-level navigation
# from another site, so no Origin and no SameSite=Strict cookie, through the
# proxy, which names the browser's host.
NAV = {"accept": "text/html,application/xhtml+xml", "sec-fetch-site": "cross-site",
       "sec-fetch-mode": "navigate", "x-forwarded-host": HOST, "x-forwarded-proto": "https",
       "x-forwarded-for": "198.51.100.30"}
NOT_A_PAGE = {k: v for k, v in NAV.items() if k != "accept"}


def refused_by_gate(r) -> bool:
    return r.status_code in (303, 307) or (r.status_code == 403 and "access denied" in r.text)


async def main():
    L._owner_email, L._cookie_suffix = "owner@example.com", "0a1b2c3d"
    manifests.MANIFESTS_DIR = Path(ROOT) / "server" / "integrations" / "manifests"
    manifests.load_all(force=True)
    with tempfile.TemporaryDirectory() as d:
        oauth._STATE_KEY_PATH = Path(d) / "oauth-state.key"
        await run(build())


async def run(app):
    transport = httpx.ASGITransport(app=app, client=PROXY)
    async with httpx.AsyncClient(transport=transport, base_url=f"http://{HOST}",
                                 follow_redirects=False) as c:
        # 1. each return reaches its handler without a session
        signed = oauth.sign_state({"provider": "gmail", "user_id": "3", "ts": time.time(),
                                   "nonce": "never-minted"})
        r = await c.get("/api/integrations/callback/gmail", params={"code": "c", "state": signed},
                        headers=NAV)
        check(r.status_code == 400 and "invalid_state" in r.text and not refused_by_gate(r),
              "a data provider's return reaches its handler without a session; a state this Host "
              "signed but never minted is refused there", (r.status_code, r.text[:200]))
        r = await c.get("/api/gitea/oauth/callback", params={"code": "c", "state": "made-up"},
                        headers=NAV)
        check(r.status_code == 400 and "expired" in r.text and not refused_by_gate(r),
              "gitea's return reaches its handler without a session; an unknown state is refused "
              "there", (r.status_code, r.text[:200]))
        r = await c.get("/api/mcp/oauth/callback", params={"code": "c", "state": "s"}, headers=NAV)
        check(r.status_code == 400 and "sign-in" in r.text and not refused_by_gate(r),
              "an MCP server's return reaches its handler without a session",
              (r.status_code, r.text[:200]))

        # 2. nothing else is let through
        for path in ("/api/integrations/callback/gmail", "/api/gitea/oauth/callback",
                     "/api/mcp/oauth/callback"):
            r = await c.post(path, params={"code": "c", "state": "s"}, headers=NOT_A_PAGE)
            check(r.status_code == 403, f"POST {path} without a session: refused by the gate",
                  r.status_code)
        r = await c.get("/api/integrations/callback/_google_base", params={"code": "c", "state": "s"},
                        headers=NOT_A_PAGE)
        check(r.status_code == 403, "a provider path that is no provider key: refused by the gate",
              r.status_code)
        looked_up = []
        real_import = I._import_provider
        I._import_provider = lambda key: looked_up.append(key) or real_import(key)
        try:
            r = await c.get("/api/integrations/callback/nosuchprovider",
                            params={"code": "c", "state": "s"}, headers=NAV)
        finally:
            I._import_provider = real_import
        check(r.status_code == 404 and looked_up == [],
              "a provider with no manifest: 404 before any module is looked up",
              (r.status_code, looked_up))
        r = await c.get("/api/integrations/connections", headers=NAV)
        check(refused_by_gate(r), "the rest of /api/integrations still needs a session", r.status_code)

        # 3. gitea: a PKCE entry this Host made, once, under ten minutes
        exchanged = []

        async def fake_exchange(netloc, form):
            exchanged.append(form["code"])
            return {"access_token": "at-" + form["code"], "refresh_token": "rt", "expires_in": 3600}

        async def fake_persist():
            return None
        saved = (GT.gitea_oauth_exchange, GT._persist_user_tok, GT.gitea_netloc)
        GT.gitea_oauth_exchange, GT._persist_user_tok = fake_exchange, fake_persist
        GT.gitea_netloc = lambda: "git.example.test"
        try:
            GT._pkce["s-live"] = {"verifier": "v", "ts": time.time(),
                                  "redirect_uri": f"https://{HOST}/api/gitea/oauth/callback"}
            r1 = await c.get("/api/gitea/oauth/callback", params={"code": "c1", "state": "s-live"},
                             headers=NAV)
            r2 = await c.get("/api/gitea/oauth/callback", params={"code": "c2", "state": "s-live"},
                             headers=NAV)
            check(r1.status_code == 200 and r2.status_code == 400 and exchanged == ["c1"],
                  "gitea: a state this Host made trades one code, once",
                  (r1.status_code, r2.status_code, exchanged))
            GT._pkce["s-old"] = {"verifier": "v", "redirect_uri": "x",
                                 "ts": time.time() - GT._PKCE_TTL - 1}
            r = await c.get("/api/gitea/oauth/callback", params={"code": "c3", "state": "s-old"},
                            headers=NAV)
            check(r.status_code == 400 and exchanged == ["c1"] and "s-old" not in GT._pkce,
                  "gitea: a state past ten minutes is refused, with no exchange",
                  (r.status_code, exchanged))
            r = await c.get("/api/gitea/oauth/callback", params={"code": "c4", "state": ""},
                            headers=NAV)
            check(r.status_code == 400 and exchanged == ["c1"], "gitea: no state, no exchange",
                  r.status_code)
        finally:
            GT.gitea_oauth_exchange, GT._persist_user_tok, GT.gitea_netloc = saved
            GT._user_tok.clear()

    # The predicate itself, as the gates and the front door read it.
    check(G.is_oauth_callback("/api/integrations/callback/youtube")
          and G.is_oauth_callback("/api/gitea/oauth/callback")
          and G.is_oauth_callback("/api/mcp/oauth/callback"),
          "the three OAuth returns are OAuth returns")
    check(not any(G.is_oauth_callback(p) for p in (
        "/api/integrations/callback/", "/api/integrations/callback/a/b",
        "/api/integrations/callbacks/x", "/api/integrations/callback/A",
        "/api/integrations/callback/" + "a" * 65, "/api/gitea/oauth/callback/x",
        "/api/mcp/oauth/start")),
          "nothing else is")


asyncio.run(main())
sys.exit(failed)
