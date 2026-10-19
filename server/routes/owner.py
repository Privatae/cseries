"""Setup and sign-in pages for the owner of a standalone Host.

The pages are plain HTML with a small inline script, so they work before any
app or dashboard bundle has loaded. server/owner_login.py holds the rules;
these routes only carry the form and set or clear the cookie.
"""

from __future__ import annotations

import logging

from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from server import owner_login, trusted_proxies
from server.home_origin import HOME
from server.middleware.localhost_only import _unmap_v4
from server.middleware.owner_session import (_cookie, join_url, live_session, own_app_origin,
                                              request_host)

router = APIRouter()
logger = logging.getLogger("cseries.owner_login")

_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} · C-Series</title>
<style>
 body{{font-family:system-ui,sans-serif;max-width:24rem;margin:12vh auto;padding:0 1rem;color:#1b1b1b}}
 h1{{font-size:1.4rem}} label{{display:block;margin:.8rem 0 .2rem}}
 input{{width:100%;padding:.55rem;font-size:1rem;box-sizing:border-box}}
 button{{margin-top:1.2rem;padding:.6rem 1rem;font-size:1rem;width:100%}}
 p.note{{color:#555;font-size:.9rem}} p#err{{color:#b00020}}
</style></head><body>
<h1>{title}</h1>
{intro}
<form id="f">{fields}<button type="submit">{button}</button></form>
<p id="err" role="alert"></p>
<script>
document.getElementById('f').addEventListener('submit', async (e) => {{
  e.preventDefault();
  const body = Object.fromEntries(new FormData(e.target));
  const r = await fetch('{action}', {{method: 'POST', headers: {{'Content-Type': 'application/json'}},
                                      body: JSON.stringify(body), credentials: 'same-origin'}});
  const j = await r.json().catch(() => ({{}}));
  if (r.ok) {{ location.href = '/'; }} else {{ document.getElementById('err').textContent = j.error || 'Something went wrong.'; }}
}});
</script></body></html>"""

_EMAIL = '<label for="email">Email</label><input id="email" name="email" type="email" autocomplete="username" required>'


def _client(request: Request) -> str:
    """Who is signing in, for the wrong-password backoff only. Behind a proxy
    every visitor arrives from the proxy, so a backoff keyed on the peer would
    let one attacker lock the owner out. When the peer is a trusted proxy, the
    nearest X-Forwarded-For address that is not itself a trusted proxy is used
    instead. Never used to decide access: the gates read the socket peer."""
    peer = _unmap_v4(request.client.host if request.client else "") or "?"
    proxies = trusted_proxies.networks()
    if not proxies or not trusted_proxies.contains(peer, proxies):
        return peer
    hops = [h.strip() for h in (request.headers.get("x-forwarded-for") or "").split(",") if h.strip()]
    for hop in reversed(hops):
        hop = _unmap_v4(hop) or hop
        if not trusted_proxies.contains(hop, proxies):
            return hop
    return peer


def _browser_used_https(request: Request) -> bool:
    """Whether the browser reached this Host over HTTPS. Behind a proxy that
    terminates TLS this hop is plain HTTP, and only the proxy's header says
    otherwise (server/launch.py turns uvicorn's own reading of it off). The
    header is believed from anyone because it can only add Secure: a cookie
    marked Secure on a plain-HTTP page is dropped by the browser, which costs
    a forger their own sign-in and nothing else."""
    if request.url.scheme == "https":
        return True
    xfp = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
    if xfp == "https":
        return True
    first = (request.headers.get("forwarded") or "").split(",")[0]
    return any(p.strip().lower() == "proto=https" for p in first.split(";"))


def _with_cookie(resp, token: str, request: Request):
    resp.set_cookie(owner_login.cookie_name(), token,
                    max_age=int(owner_login.SESSION_MAX.total_seconds()),
                    httponly=True, samesite="strict",
                    secure=_browser_used_https(request), path="/")
    return resp


async def _form(request: Request) -> dict:
    """The JSON object the page posted, or {} for anything else, so a
    malformed body is a refusal rather than a server error."""
    try:
        body = await request.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _text(body: dict, key: str) -> str:
    v = body.get(key)
    return v if isinstance(v, str) else ""


@router.get("/setup", include_in_schema=False)
async def setup_page():
    if not owner_login.needs_setup():
        return RedirectResponse("/login", status_code=303)
    return HTMLResponse(_PAGE.format(
        title="Set up C-Series",
        intro=('<p class="note">Create the owner account. The setup code is printed '
               "in this Host's log (for example <code>docker compose logs host</code>).</p>"),
        fields=('<label for="code">Setup code</label><input id="code" name="code" '
                'autocomplete="one-time-code" required>' + _EMAIL +
                f'<label for="password">Password (at least {owner_login.MIN_PASSWORD_LEN} '
                'characters)</label><input id="password" name="password" type="password" '
                'autocomplete="new-password" required>'),
        button="Create owner", action="/api/owner/setup"),
        headers={"Cache-Control": "no-store"})


@router.get("/login", include_in_schema=False)
async def login_page():
    if owner_login.needs_setup():
        return RedirectResponse("/setup", status_code=303)
    return HTMLResponse(_PAGE.format(
        title="Sign in to C-Series", intro="",
        fields=(_EMAIL + '<label for="password">Password</label><input id="password" '
                'name="password" type="password" autocomplete="current-password" required>'),
        button="Sign in", action="/api/owner/login"),
        headers={"Cache-Control": "no-store"})


@router.get("/api/owner/status", include_in_schema=False)
async def owner_status(request: Request):
    signed_in = bool((await live_session(request.scope))[0])
    return {"setup_needed": owner_login.needs_setup(), "signed_in": signed_in}


@router.post("/api/owner/setup", include_in_schema=False)
async def owner_setup(request: Request):
    body = await _form(request)
    try:
        await owner_login.create_owner(_text(body, "code"), _text(body, "email"),
                                       _text(body, "password"))
        token = await owner_login.sign_in(_text(body, "email"), _text(body, "password"),
                                          _client(request), request_host(request.scope))
    except owner_login.LoginError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return _with_cookie(JSONResponse({"ok": True}), token, request)


@router.post("/api/owner/login", include_in_schema=False)
async def owner_sign_in(request: Request):
    body = await _form(request)
    try:
        token = await owner_login.sign_in(_text(body, "email"), _text(body, "password"),
                                          _client(request), request_host(request.scope))
    except owner_login.LoginError as exc:
        return JSONResponse({"error": str(exc)}, status_code=401)
    return _with_cookie(JSONResponse({"ok": True}), token, request)


@router.post("/api/owner/logout", include_in_schema=False)
async def owner_sign_out(request: Request):
    await owner_login.sign_out(_cookie(request.scope))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(owner_login.cookie_name(), path="/")
    return resp


# ── app-origin sessions (server/owner_login.py start_join / finish_join) ────
#
# An origin-isolated app is framed from its own host, which the desktop's
# host-only cookie never reaches. Its first page load comes to the session
# gate without a session and is sent to the desktop's /__host/join, which
# (with the desktop's session) makes a one-time code for that app host and
# sends the frame on to the app host's /__host/session, which redeems it for
# a session cookie of the app host's own and returns the frame to its page.
# Every hop is the same site, so the SameSite=Strict cookie rides each one.

def _safe_rd(rd: str | None) -> str:
    """A path on the same origin to return to, or "/"."""
    rd = rd or "/"
    if not rd.startswith("/") or rd.startswith("//") or "\\" in rd or any(ord(c) < 0x20 for c in rd):
        return "/"
    return rd


def _no_store(resp):
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


@router.get("/__host/renew", include_in_schema=False)
async def host_renew(request: Request):
    """204 while this origin's session is live, else 401 (app-frame-init.js)."""
    email, _token = await live_session(request.scope)
    return _no_store(Response(status_code=204 if email else 401))


@router.get("/__host/join", include_in_schema=False)
async def host_join(request: Request, to: str = "", rd: str = "/"):
    """On the desktop, with its session: send the frame to app origin `to`
    with a one-time code for a session there."""
    if request_host(request.scope) != HOME.host:
        return _no_store(JSONResponse({"error": "not the desktop"}, status_code=404))
    _email, token = await live_session(request.scope)
    if not token:
        return _no_store(JSONResponse({"error": "sign in first"}, status_code=401))
    target = urlsplit(to)
    try:
        port = target.port
    except ValueError:
        port = -1
    host = (target.hostname or "").lower()
    if target.scheme != HOME.scheme or port != HOME.port or not own_app_origin(host) \
            or HOME.origin_of(host) != to.rstrip("/"):
        return _no_store(JSONResponse({"error": "not an app origin of this Host"}, status_code=400))
    code = owner_login.start_join(token, host)
    return _no_store(RedirectResponse(
        HOME.origin_of(host) + "/__host/session?" + urlencode({"code": code, "rd": _safe_rd(rd)}),
        status_code=303))


@router.get("/__host/session", include_in_schema=False)
async def host_session(request: Request, code: str = "", rd: str = "/"):
    """On an app origin: redeem a join code for this origin's own session,
    or, without a usable one, go and get one from the desktop. Anywhere else
    (the desktop: session-keeper.js's Continue after a session ends): back to
    `rd` while signed in, else to the sign-in page."""
    host = request_host(request.scope)
    rd = _safe_rd(rd)
    if not own_app_origin(host):
        email, _token = await live_session(request.scope)
        target = rd if email else ("/setup" if owner_login.needs_setup() else "/login")
        return _no_store(RedirectResponse(target, status_code=303))
    token = await owner_login.finish_join(code, host) if code else None
    if not token:
        return _no_store(RedirectResponse(join_url(host, rd), status_code=303))
    return _no_store(_with_cookie(RedirectResponse(rd, status_code=303), token, request))
