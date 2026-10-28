#!/usr/bin/env python3
# gate-scope: whole-tree — every system app
"""Gate: one web origin per isolated app.

Every isolated app used
to be framed from ONE origin per Host (apps-<handle>), so a co-open community
app could read the Settings frame's recovery key and window token. Each now has
its own origin, apps-<key>-<handle>. This drives the REAL code of every piece
that makes that hold, over the real token codec:

  1. THE KEY (applications/app_origin.py origin_key). Pinned vectors: changing
     the recipe moves every app's origin and empties its browser storage, so a
     change must be loud. 20 characters of [a-z2-7] (100 bits), distinct for
     every app id this repo ships and every reserved id.
  2. THE LABEL GRAMMAR (classify) — the SAME vectors as the direct-router's
     test_app_origin_labels.py, so the kernel and the router cannot disagree
     about which name is which: desktop, per-app, legacy shared, malformed.
  3. WHAT AN APP ORIGIN SERVES (middleware/app_origin_guard.py, the real ASGI
     middleware): its own /system-apps/<id>/, /api, the shared files — nothing
     else, on HTTP and on a WebSocket upgrade. Not another app's files, not the
     Host's project files, not the desktop. The legacy shared origin answers
     nothing. The overlay serves custom/ files nowhere on an app origin.
  4. WHO MAY FRAME IT (response_csp): only this Host's desktop, with a
     community app's exfil wall kept whole; /api responses are sandboxed.
  5. WHICH TOKEN AN ORIGIN TAKES (applications/middleware.py, the real
     ApplicationTokenMiddleware in a real app, and proxy._require_own_token for
     the WebSocket path): a token minted on Settings' origin only there; a
     desktop token on no app origin; another app's token nowhere but its own.
  6. THE RESIDUAL PER-APP ORIGINS LEFT OPEN: a page on the desktop's own origin (Circles,
     the Workbench, a Host-built app) asking /launch for Settings' token gets
     NO token; the token is minted only on Settings' own origin
     (routes/applications.py frame_token), which refuses the desktop, other
     apps' origins and cross-origin pages.
  7. COLLISIONS (keys_for, installer._refuse_origin_key_collision): two ids
     with one key never share an origin; image-baked apps win.
  8. THE DESKTOP'S HALF (dashboard/base/js/app-runner.js frameOrigin, run in
     node): it builds the same host the kernel computes, and refuses to frame
     an isolated app with no valid key.

Mutations run against the code under test (each applied to a copy of the tree,
this file run, then discarded; the number is failing checks):

  key recipe prefix app-origin/v1 -> v2                       red, 4
  KEY_LEN 20 -> 16                                            red, 15
  path_allowed always True for an app origin                  red, 22
  path_allowed: /system-apps/<id> key comparison dropped      red, 6
  guard passes the legacy shared origin through               red, 7
  guard lets a WebSocket through instead of closing it        red, 2
  response_csp leaves frame-ancestors zone-wide               red, 4
  response_csp drops the /api sandbox                         red, 2
  token_fits_origin: unbound tokens accepted on app origins   red, 3
  token_fits_origin: bound tokens accepted anywhere           red, 3
  token middleware binding check removed                      red, 6
  launch_app hands browsers an isolated app's token           red, 4
  frame_token origin check removed                            red, 4
  frame_token cross-origin-browser check removed              red, 1
  frame_token is_isolated check removed                       red, 1
  keys_for ranking ignores provenance                         red, 1
  installer collision refusal removed                         red, 1
  overlay_allowed always True                                 red, 1
  app-runner frameOrigin key regex dropped                    red, 1
  proxy._require_own_token origin check removed               red, 2
  app_files_here: the desktop serves an isolated app's pages   red, 11
  app_files_here: desktop images served unsandboxed            red, 4
  app_files_here: in-pod callers refused                      red, 2
  serve_app ignores the decision                              red, 2
  serve_app answers an image path with a directory's index    red, 1
  serve_app drops the image headers                           red, 1
  serve_app_ws ignores the decision                           red, 2
  the token middleware reads the app_token cookie again       red, 6

The zone is the one CSERIES_URL names (server/home_origin.py): this file sets
http://home.cseries.localhost:8800 before any server module is imported, so
every name below is <label>.cseries.localhost and every frame-ancestors source
is http://...:8800. A name in any OTHER zone (.host.example.test, say) is
not an address of this Host at all.

Run: APP_ROOT=<repo> python3 scripts/test_app_origin.py
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(os.environ.get("APP_ROOT", Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(ROOT))
# The home zone is read once, when server.home_origin is first imported, so
# it is fixed here, ahead of every server import, and not left to the runner.
os.environ["CSERIES_URL"] = "http://home.cseries.localhost:8800"

failures: list[str] = []


def check(label, ok, detail=""):
    print(f"[app-origin] {'ok  ' if ok else 'FAIL'} {label}"
          + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(label)


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


from fastapi import HTTPException  # noqa: E402

from server.applications import app_origin as ao  # noqa: E402
from server.applications import tokens  # noqa: E402

H = "0123456789abcdef"
ZONE = ".cseries.localhost"
PORT = 8800
DESKTOP = f"{H}{ZONE}"
LEGACY = f"apps-{H}{ZONE}"         # the old shared origin, refused everywhere
# What the pod's own Host header carries behind a proxy; the browser's name
# arrives in X-Forwarded-Host, which wins (browser_host).
POD_HOST = "pod-0.pod.svc.cluster.local"
SECRET = b"s" * 32


def app_host(app_id, handle=H):
    return f"apps-{ao.origin_key(app_id)}-{handle}{ZONE}"


# ── 1. the key ────────────────────────────────────────────────────────────
VECTORS = {
    "settings": "a5nyu3ne5pjnbhhtbfvr",
    "backups": "ojdtvbqzo2frn62tvhih",
    "orchestrator": "j26dfjf76fx5nhlbbt7k",
    "national-weather": "4kuswejbyjrf43q5ncb4",
}
for app_id, want in VECTORS.items():
    check(f"pinned key for {app_id}", ao.origin_key(app_id) == want, ao.origin_key(app_id))

from server.applications.kernel import SDK_MANAGED_APPS  # noqa: E402
from server.applications.manifest import RESERVED_APP_IDS  # noqa: E402

ids = set(SDK_MANAGED_APPS) | set(RESERVED_APP_IDS) | {
    p.name for p in (ROOT / "system-apps").iterdir() if p.is_dir() and not p.name.startswith("_")}
keys = {i: ao.origin_key(i) for i in ids}
check("every key is 20 characters of [a-z2-7]",
      all(re.fullmatch(r"[a-z2-7]{20}", k) for k in keys.values()),
      [i for i, k in keys.items() if not re.fullmatch(r"[a-z2-7]{20}", k)])
check(f"{len(ids)} shipped and reserved app ids have {len(ids)} distinct keys",
      len(set(keys.values())) == len(ids))
check("a 16-hex handle leaves room: the per-app label is at most 63",
      len(app_host("settings").split(".")[0]) <= 63 and ao.MAX_APP_HANDLE_LEN == 37)

# ── 2. the label grammar (same vectors as the router's test) ─────────────
K = ao.origin_key("settings")
GOOD = [  # label -> (kind, handle)
    (H, (ao.DESKTOP, H)), ("alex", (ao.DESKTOP, "alex")), ("apps", (ao.DESKTOP, "apps")),
    ("a" * 63, (ao.DESKTOP, "a" * 63)),
    (f"apps-{K}-{H}", (ao.APP, H)), (f"apps-{K}-{'b' * 37}", (ao.APP, "b" * 37)),
    (f"apps-{K}-x", (ao.APP, "x")),
    (f"apps-{H}", (ao.LEGACY_APP, H)), ("apps-alex", (ao.LEGACY_APP, "alex")),
    (f"apps-{'c' * 58}", (ao.LEGACY_APP, "c" * 58)),
]
BAD = ["apps-", f"apps--{H}", f"apps-{K[:19]}-{H}", f"apps-{K}a-{H}", f"apps-{K[:19]}0-{H}",
       f"apps-{K[:19]}1-{H}", f"apps-{K[:19]}8-{H}", f"apps-{K[:19]}9-{H}", f"apps-{K}-",
       f"apps-{K}--{H}", f"apps-{K}-{H}-x", f"apps-{K}-{'b' * 38}", f"apps-{'c' * 59}",
       f"apps_{H}", "xn--80ak6aa92e", f"аpps-{K}-{H}", "+15555550100", "-" + H, H + "-",
       "a" * 64]
for label, (kind, handle) in GOOD:
    o = ao.classify(label + ZONE)
    check(f"{label + ZONE} is {kind} of {handle}",
          (o.kind, o.handle, o.zone) == (kind, handle, ZONE), repr(o))
for label in BAD:
    o = ao.classify(label + ZONE)
    check(f"{label + ZONE} is no address", o.kind == ao.BAD_APP, repr(o))
for host in ("127.0.0.1", "pod-0.pod.svc.cluster.local", f"apps-{K}-{H}.example.com",
             f"{H}{ZONE}.example.com", ""):
    check(f"{host!r} is not a zone name", ao.classify(host).kind == ao.OTHER)
# Another zone's names are not this Host's addresses, whatever their shape: the
# platform's old zones are gone, and a name built like ours in one of them must
# not pass for a desktop, an app origin or even the retired shared origin.
FOREIGN = [f"apps-{K}-{H}.host.example.test", f"{H}.host.example.test",
           f"apps-{H}.hosts.example.test", f"apps-{K}-{H}.hosts.example.test"]
check("a host in a FOREIGN zone (apps-<key>-<handle>.host.example.test) classifies as OTHER",
      all(ao.classify(h).kind == ao.OTHER for h in FOREIGN),
      [(h, ao.classify(h).kind) for h in FOREIGN if ao.classify(h).kind != ao.OTHER])
check("the per-app origin carries its key",
      ao.classify(app_host("settings")).key == K)

# browser_host: XFH first hop wins, port and case dropped; Host is the fallback.
for headers, want in (
        ({"host": POD_HOST, "x-forwarded-host": app_host("settings")},
         app_host("settings")),
        ({"host": "x", "x-forwarded-host": app_host("settings").upper() + f":{PORT}, edge.internal"},
         app_host("settings")),
        ({"host": app_host("backups")}, app_host("backups")),
        ({"host": "x", "x-forwarded-host": "  "}, "x")):
    check(f"browser_host({headers}) == {want}", ao.browser_host(headers) == want,
          ao.browser_host(headers))
raw = [(b"Host", b"pod.svc"), (b"X-Forwarded-Host", app_host("settings").encode())]
check("browser_host reads raw ASGI headers the same way", ao.browser_host(raw) == app_host("settings"))

check("app_host builds the per-app host from the desktop host",
      ao.app_host("settings", DESKTOP) == app_host("settings"), ao.app_host("settings", DESKTOP))
check("app_host drops the desktop's port: the name carries none",
      ao.app_host("settings", f"{DESKTOP}:{PORT}") == app_host("settings"),
      ao.app_host("settings", f"{DESKTOP}:{PORT}"))
check("app_host refuses a desktop named in another zone",
      ao.app_host("settings", f"{H}.host.example.test") is None
      and ao.app_host("settings", f"{H}.hosts.example.test") is None)
check("app_host refuses a handle with no room for the key",
      ao.app_host("settings", ("d" * 38) + ZONE) is None)
check("app_host refuses a non-desktop host", ao.app_host("settings", app_host("backups")) is None)

# ── 3. the guard (real ASGI middleware) ───────────────────────────────────
from server.middleware.app_origin_guard import AppOriginGuard  # noqa: E402


def guard_call(path, host, *, ws=False, xfh=True):
    seen = {}

    async def inner(scope, receive, send):
        seen["scope"] = scope
        if scope["type"] == "websocket":
            await send({"type": "websocket.accept"})
            return
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"inner"})

    sent = []

    async def send(msg):
        sent.append(msg)

    async def receive():
        return {"type": "websocket.connect"} if ws else {"type": "http.request", "body": b""}

    headers = [(b"host", POD_HOST.encode()), (b"x-forwarded-host", host.encode())] \
        if xfh else [(b"host", host.encode())]
    scope = {"type": "websocket" if ws else "http", "path": path, "headers": headers}
    run(AppOriginGuard(inner)(scope, receive, send))
    status = (sent[0].get("status") if sent and sent[0]["type"] == "http.response.start"
              else ("close" if sent and sent[0]["type"] == "websocket.close" else
                    "accept" if sent else None))
    return status, seen.get("scope"), sent


ST = app_host("settings")
for path, want in (("/system-apps/settings/", 200), ("/system-apps/settings/index.html", 200),
                   ("/system-apps/settings", 200), ("/api/backup/restore/sidecar", 200),
                   ("/api/applications/settings/frame-token", 200), ("/css/tokens.css", 200),
                   ("/fonts/inter-var-latin.woff2", 200), ("/js/app-sdk.js", 200),
                   ("/js/app-frame-init.js", 200), ("/js/model-picker.js", 200),
                   ("/system-apps/gitea/icon.png", 200), ("/system-apps/backups/img/a-1.webp", 200),
                   ("/system-apps/backups/icon.svg", 404), ("/system-apps/backups/app.js", 404),
                   ("/system-apps/backups/icon.png.html", 404), ("/system-apps/backups", 404),
                   ("/system-apps/backups/", 404), ("/system-apps/national-weather/x.html", 404),
                   ("/", 404), ("/index.html", 404), ("/js/desktop.js", 404),
                   ("/user-apps/p/index.html", 404), ("/ws", 404), ("/css/x/y.css", 404),
                   ("/system-apps/", 404), ("/apix", 404)):
    status, scope, sent = guard_call(path, ST)
    check(f"Settings' origin: {path} -> {want}", status == want, f"got {status}")
    if want == 404 and sent:
        hdrs = dict(sent[0]["headers"])
        check(f"  ... the refusal is sandboxed and nosniff ({path})",
              b"sandbox" in hdrs.get(b"content-security-policy", b"")
              and hdrs.get(b"x-content-type-options") == b"nosniff")
status, scope, _ = guard_call("/css/tokens.css", ST)
check("an allowed request is marked for the overlay (scope['app_origin'])",
      scope is not None and scope.get("app_origin") and scope["app_origin"].key == K)
check("... so the static mount does not serve custom/ there",
      scope is not None and ao.overlay_allowed(scope) is False and ao.overlay_allowed({}) is True)
status, _, _ = guard_call("/system-apps/settings/", ST, xfh=False)
check("Host alone (no router) is read the same way", status == 200)
status, _, _ = guard_call("/system-apps/backups/", ST, xfh=False)
check("... and refuses the same way", status == 404)
for path in ("/system-apps/settings/", "/api/x", "/css/tokens.css"):
    status, _, sent = guard_call(path, LEGACY)
    check(f"legacy shared origin {LEGACY}: {path} -> 404", status == 404, f"got {status}")
status, _, sent = guard_call("/system-apps/settings/", f"apps-{K[:19]}-{H}{ZONE}")
check("a malformed apps- label is refused", status == 404, f"got {status}")
for host in (DESKTOP, "127.0.0.1:8800", "cseries.entity-9003.svc.cluster.local"):
    status, scope, _ = guard_call("/user-apps/p/index.html", host)
    check(f"{host}: the desktop / in-pod caller is untouched", status == 200 and "app_origin" not in scope)
status, _, _ = guard_call("/system-apps/backups/ws", ST, ws=True)
check("WebSocket to another app's path on Settings' origin is closed", status == "close", f"got {status}")
status, _, _ = guard_call("/system-apps/settings/ws", ST, ws=True)
check("WebSocket to Settings' own path is let through", status == "accept", f"got {status}")
status, _, _ = guard_call("/system-apps/settings/ws", LEGACY, ws=True)
check("WebSocket on the legacy shared origin is closed", status == "close", f"got {status}")

# ── 4. who may frame it ───────────────────────────────────────────────────
o = ao.classify(ST)
# One source: this Host's own desktop, on the desktop's scheme AND port (a
# source without a port matches only the scheme's default one).
FA = f"frame-ancestors http://{H}{ZONE}:{PORT}"
from server.applications.proxy import _app_csp  # noqa: E402
community = _app_csp(["api.weather.gov"])
pinned = ao.response_csp(o, "/system-apps/national-weather/", community)
check("a community app's CSP keeps its exfil wall", "connect-src 'self' https://api.weather.gov" in pinned
      and "form-action 'self'" in pinned and "object-src 'none'" in pinned, pinned)
check("... and frame-ancestors names only this Host's desktop",
      pinned.endswith(FA) and "*." not in pinned.split("frame-ancestors", 1)[1], pinned)
check("... with exactly one frame-ancestors directive", pinned.count("frame-ancestors") == 1, pinned)
html = ao.response_csp(o, "/system-apps/settings/", "")
check("a first-party app page gets only the pinned frame-ancestors", html == FA, html)
api = ao.response_csp(o, "/api/backup/restore/sidecar", "")
check("/api on an app origin is sandboxed (no script runs with the app's origin)",
      api.startswith("sandbox allow-downloads; default-src 'none'") and api.endswith(FA), api)
check("the shared files are sandboxed too",
      ao.response_csp(o, "/css/tokens.css", "").startswith("sandbox"))
check("another app's image served here is sandboxed",
      ao.response_csp(o, "/system-apps/gitea/icon.png", "").startswith("sandbox"))
check("this app's own files are not",
      not ao.response_csp(o, "/system-apps/settings/app.js", "").startswith("sandbox"))
main_src = (ROOT / "server" / "main.py").read_text()
sh = main_src[main_src.index("async def security_headers"):main_src.index("async def security_headers") + 2500]
check("main.py security_headers applies app_origin.response_csp on an app origin",
      "app_origin.response_csp(" in sh and "origin.kind == app_origin.APP" in sh)
check("... and stamps X-Frame-Options only off app origins",
      sh.index("origin.kind == app_origin.APP")
      < sh.index('response.headers["X-Frame-Options"] = "SAMEORIGIN"'))
i_guard = main_src.index("app.add_middleware(AppOriginGuard)")
i_host = main_src.index("app.add_middleware(HostHeaderGuard)")
check("the guard is installed just inside the Host header gate (outside everything else)",
      i_guard < i_host and "add_middleware(" not in main_src[i_guard + 10:i_host]
      and "@app.middleware" not in main_src[i_guard:])
ov = main_src[main_src.index("class OverlayStaticFiles"):]
check("the overlay consults app_origin.overlay_allowed before serving custom/",
      "custom_path.is_file() and app_origin.overlay_allowed(scope)" in ov)

# ── 5. which token an origin takes ────────────────────────────────────────
bound, bound_tok = tokens.mint(SECRET, app_id="settings", permissions=["read:backup_settings"],
                               origin_key=K)
bound_b, _ = tokens.mint(SECRET, app_id="backups", permissions=["read:backup_settings"],
                         origin_key=ao.origin_key("backups"))
unbound, _ = tokens.mint(SECRET, app_id="settings", permissions=["read:backup_settings"])
check("a bound token round-trips its origin key", tokens.verify(SECRET, bound).origin_key == K)
check("an unbound token's body has no origin_key field (old shape, byte-compatible)",
      "origin_key" not in json.loads(tokens._b64u_decode(unbound.split(".")[0])))

from fastapi import Depends, FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from server.applications.middleware import ApplicationTokenMiddleware, requires_permission  # noqa: E402

mini = FastAPI()
mini.state.kernel_secret = SECRET
mini.add_middleware(ApplicationTokenMiddleware)


@mini.get("/api/backup/restore/sidecar")
async def _sidecar(_: None = Depends(requires_permission("read:backup_settings"))):
    return {"ok": True}


@mini.get("/api/applications")
async def _app_list():           # a header-less bootstrap read, like the real one
    return {"applications": []}


client = TestClient(mini)


def api(host, tok=None, cookie=False):
    h = {"host": POD_HOST, "x-forwarded-host": host} if host else {}
    if tok and not cookie:
        h["X-Application-Token"] = tok
    if tok and cookie:
        h["cookie"] = f"app_token={tok}"
    return client.get("/api/backup/restore/sidecar", headers=h).status_code


for label, host, tok, want in (
        ("Settings' own token on Settings' origin", ST, bound, 200),
        ("Settings' own token on the desktop", DESKTOP, bound, 403),
        ("Settings' own token on Backups' origin", app_host("backups"), bound, 403),
        ("Settings' own token in-pod (no zone name)", None, bound, 403),
        ("Backups' token on Settings' origin", ST, bound_b, 403),
        ("a desktop-minted Settings token on Settings' origin", ST, unbound, 403),
        ("a desktop-minted Settings token on the desktop (scripts, the CLI)", DESKTOP, unbound, 200),
        ("a desktop-minted token in-pod", None, unbound, 200)):
    got = api(host, tok)
    check(f"{label} -> {want}", got == want, f"got {got}")
# A cookie is the one token source a page on ANOTHER origin can plant (any
# page in the zone can set one for the whole zone), and nothing of ours sets
# it. It is no longer read at all: it neither authenticates nor locks out.
check("a cookie token never authenticates, even on its own origin (401, not 200)",
      api(ST, bound, cookie=True) == 401 and api(DESKTOP, unbound, cookie=True) == 401)
check("a cookie token that does not fit the origin is ignored, never a 403",
      api(ST, unbound, cookie=True) == 401 and api(DESKTOP, bound, cookie=True) == 401)


def bootstrap(host, cookie):
    return client.get("/api/applications", headers={
        "host": POD_HOST, "x-forwarded-host": host,
        "cookie": f"app_token={cookie}"}).status_code


for _label, _host in (("the desktop", DESKTOP), ("Settings' origin", ST),
                      ("Backups' origin", app_host("backups"))):
    check(f"a planted zone-wide app_token cookie does not lock {_label} out "
          f"(header-less app list: 200)",
          bootstrap(_host, bound) == 200 and bootstrap(_host, unbound) == 200
          and bootstrap(_host, bound_b) == 200)
check("a header token on the wrong origin is still a 403",
      api(ST, unbound) == 403 and api(DESKTOP, bound) == 403)
mw_src = (ROOT / "server" / "applications" / "middleware.py").read_text()
check("the token middleware reads the header only (no cookie read left)",
      "request.cookies.get(COOKIE_NAME)" not in mw_src
      and "token_str = request.headers.get(HEADER_NAME)\n" in mw_src)

from server.applications import proxy  # noqa: E402


async def _secret(_pool):
    return SECRET


real_get_secret = tokens.get_secret
tokens.get_secret = _secret
try:
    def own_token(host, tok, app_id="settings"):
        req = types.SimpleNamespace(headers={"x-forwarded-host": host, "X-Application-Token": tok}
                                    if host else {"X-Application-Token": tok})
        try:
            run(proxy._require_own_token(None, app_id, req))
            return 200
        except HTTPException as e:
            return e.status_code
    check("backend gate: Settings' token on its own origin passes", own_token(ST, bound) == 200)
    check("backend gate (the WebSocket path): Settings' token off its origin is refused",
          own_token(DESKTOP, bound) == 403)
    check("backend gate: a desktop token on Settings' origin is refused", own_token(ST, unbound) == 403)
finally:
    tokens.get_secret = real_get_secret

# ── 5b. an isolated app's pages are not served on the DESKTOP's origin ──────
# The one decision (app_origin.app_files_here), then the real serve_app and
# serve_app_ws over a real install dir, for each kind of caller.
IN_POD = "entity-9003-0.entity-9003.entity-9003.svc.cluster.local:8800"   # the page checks
for (app_id, isolated, host, rest, want) in (
        ("national-weather", True, DESKTOP, "index.html", ao.REFUSE),
        ("national-weather", True, DESKTOP, "", ao.REFUSE),
        ("national-weather", True, DESKTOP, "assets/app.js", ao.REFUSE),
        ("national-weather", True, DESKTOP, "icon.svg", ao.IMAGE),
        ("national-weather", True, DESKTOP, "icon-64.png", ao.IMAGE),
        ("settings", True, DESKTOP, "index.html", ao.REFUSE),
        ("sites", True, DESKTOP, "icon.svg", ao.IMAGE),
        ("circles", False, DESKTOP, "index.html", ao.SERVE),
        ("my-tool", False, DESKTOP, "app.js", ao.SERVE),
        ("national-weather", True, app_host("national-weather"), "index.html", ao.SERVE),
        ("national-weather", True, ST, "index.html", ao.REFUSE),
        ("national-weather", True, ST, "icon.png", ao.IMAGE),
        ("national-weather", True, ST, "icon.svg", ao.REFUSE),
        ("national-weather", True, IN_POD.split(":")[0], "index.html", ao.SERVE),
        ("national-weather", True, LEGACY, "index.html", ao.REFUSE)):
    got = ao.app_files_here(app_id, isolated, ao.classify(host), rest)
    check(f"app_files_here({app_id}, isolated={isolated}, {host[:28]}…, {rest!r}) == {want}",
          got == want, got)

import tempfile as _tempfile  # noqa: E402
from server import brain_service as _bs  # noqa: E402
from server.applications import registry as _reg  # noqa: E402

_root = Path(_tempfile.mkdtemp(prefix="app-origin-serve-"))
for _a, _prov in (("national-weather", "community"), ("circles", "standard")):
    (_root / _a).mkdir()
    (_root / _a / "app.yaml").write_text(f"id: {_a}\nprovenance: {_prov}\nruntime:\n  kind: static\n")
    (_root / _a / "index.html").write_text(f"<html><head></head><body>{_a} page</body></html>")
    (_root / _a / "icon.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"></svg>')
    (_root / _a / "trick.png").mkdir()      # a DIRECTORY with an image's name
    (_root / _a / "trick.png" / "index.html").write_text("<script>parent.x=1</script>")
_rows = {"national-weather": "community", "circles": "standard"}


async def _reg_get(_pool, app_id):
    if app_id not in _rows:
        return None
    return types.SimpleNamespace(id=app_id, provenance=_rows[app_id], status="active")


_saved = (proxy.INSTALL_ROOT, _reg.get, _bs._pool)
proxy.INSTALL_ROOT, _reg.get, _bs._pool = _root, _reg_get, object()
try:
    def serve(app_id, rest, host):
        req = types.SimpleNamespace(headers={"host": POD_HOST,
                                             "x-forwarded-host": host} if host else {"host": IN_POD})
        try:
            resp = run(proxy.serve_app(app_id, rest, req))
            return resp.status_code, resp
        except HTTPException as e:
            return e.status_code, None

    code, _ = serve("national-weather", "index.html", DESKTOP)
    check("serve_app: a community app's page on the DESKTOP's origin -> 404", code == 404, code)
    code, _ = serve("national-weather", "", DESKTOP)
    check("serve_app: ... its entry page too", code == 404, code)
    code, resp = serve("national-weather", "icon.svg", DESKTOP)
    check("serve_app: its icon.svg on the desktop -> 200, sandboxed, nosniff",
          code == 200 and resp.headers.get("content-security-policy") == "sandbox; default-src 'none'"
          and resp.headers.get("x-content-type-options") == "nosniff",
          (code, resp and dict(resp.headers)))
    code, _ = serve("national-weather", "missing.png", DESKTOP)
    check("serve_app: an image that is not on disk -> 404 (never the app's backend)", code == 404, code)
    code, resp = serve("national-weather", "trick.png", DESKTOP)
    check("serve_app: a directory named like an image is not answered with its index.html",
          code == 404, (code, resp and getattr(resp, "body", b"")[:40]))
    code, resp = serve("national-weather", "index.html", app_host("national-weather"))
    check("serve_app: the same page on the app's own origin -> 200", code == 200, code)
    code, resp = serve("national-weather", "index.html", None)
    check("serve_app: in-pod (the page checks' address, no zone name) -> 200", code == 200, code)
    code, resp = serve("circles", "index.html", DESKTOP)
    check("serve_app: a same-origin app on the desktop is unchanged -> 200", code == 200, code)

    class _WS:
        def __init__(self, host):
            self.headers = {"host": POD_HOST, "x-forwarded-host": host}
            self.closed = None

        async def close(self, code=1000, reason=""):
            self.closed = (code, reason)

    _ws = _WS(DESKTOP)
    run(proxy.serve_app_ws(_ws, "national-weather", "ws"))
    check("serve_app_ws: a community app's socket on the desktop's origin is closed 1008",
          _ws.closed and _ws.closed[0] == 1008 and "not served" in _ws.closed[1], _ws.closed)
    _ws = _WS(app_host("national-weather"))
    run(proxy.serve_app_ws(_ws, "national-weather", "ws"))
    check("serve_app_ws: on its own origin it gets past the origin check (static app: 'ws only' refusal)",
          _ws.closed and "ws only" in _ws.closed[1], _ws.closed)
finally:
    proxy.INSTALL_ROOT, _reg.get, _bs._pool = _saved
    shutil.rmtree(_root, ignore_errors=True)
proxy_src = (ROOT / "server" / "applications" / "proxy.py").read_text()
check("serve_app and serve_app_ws both ask the one decision (_files_here -> app_files_here)",
      proxy_src.count("_files_here(app_id, app, path, ") == 2
      and "app_origin.app_files_here(" in proxy_src)

# ── 6. the residual: /launch and frame_token ─────────────────────────────
from server import brain_service  # noqa: E402
from server.applications import registry  # noqa: E402
from server.routes import applications as routes  # noqa: E402


def row(app_id, provenance, installed=0):
    return registry.InstalledApp(
        id=app_id, name=app_id, version="1.0.0", provenance=provenance, entrypoint="index.html",
        permissions=["read:backup_settings"], source_dir=f"/nonexistent/{app_id}",
        source_signature=None, installed_at=datetime(2026, 1, 1, tzinfo=timezone.utc)
        + timedelta(days=installed), upgraded_at=None, status="active", app_license={},
        authored_here=None)


APPS = {a.id: a for a in (row("settings", "native"), row("backups", "standard"),
                          row("circles", "standard"), row("national-weather", "community", 5),
                          row("my-tool", "custom", 6))}


async def _get(_pool, app_id):
    return APPS.get(app_id)


async def _list_active(_pool):
    return list(APPS.values())


class FakeReq:
    def __init__(self, host, extra=None, token=None):
        self.headers = {"host": POD_HOST, "x-forwarded-host": host}
        self.headers.update(extra or {})
        self.state = types.SimpleNamespace(app_token=token)
        self.app = types.SimpleNamespace(state=types.SimpleNamespace(kernel_secret=SECRET))


def same_origin(host):
    return {"sec-fetch-site": "same-origin", "origin": f"http://{host}:{PORT}"}


real = (registry.get, registry.list_active, brain_service._pool, routes.load_manifest)
registry.get, registry.list_active = _get, _list_active
brain_service._pool = object()
routes.load_manifest = lambda _p: types.SimpleNamespace(runtime=types.SimpleNamespace(kind="static"))
try:
    def call(fn, app_id, req):
        try:
            return 200, run(fn(app_id, req))
        except HTTPException as e:
            return e.status_code, str(e.detail)

    # THE TEST THE RESIDUAL ASKED FOR: a page on the desktop's own origin asks
    # /launch for Settings' token, with the headers every browser page sends.
    code, body = call(routes.launch_app, "settings", FakeReq(DESKTOP, same_origin(DESKTOP)))
    check("a desktop-origin page asking /launch for Settings' token gets NO token",
          code == 200 and body.get("token") is None and body.get("token_delivery") == "app-origin",
          f"{code} {body}")
    check("... and nothing token-shaped is anywhere in the answer",
          code == 200 and not re.search(r"[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}", json.dumps(body)),
          json.dumps(body)[:200])
    code, body = call(routes.launch_app, "national-weather", FakeReq(DESKTOP, same_origin(DESKTOP)))
    check("... the same for a community app", code == 200 and body.get("token") is None, f"{code} {body}")
    code, body = call(routes.launch_app, "settings", FakeReq(DESKTOP, {"origin": f"http://{DESKTOP}:{PORT}"}))
    check("... Origin alone (a browser without Fetch Metadata) is a browser too",
          code == 200 and body.get("token") is None, f"{code} {body}")
    code, body = call(routes.launch_app, "circles", FakeReq(DESKTOP, same_origin(DESKTOP)))
    check("a same-origin app's /launch still returns its token (unchanged)",
          code == 200 and isinstance(body.get("token"), str) and body.get("token_delivery") == "launch",
          f"{code} {body}")
    code, body = call(routes.launch_app, "settings", FakeReq(DESKTOP))
    check("a scripted caller (no Origin, no Fetch Metadata) still gets an unbound token",
          code == 200 and isinstance(body.get("token"), str)
          and tokens.verify(SECRET, body["token"]).origin_key == "", f"{code} {body}")

    # frame_token: only on the app's own origin, only from that origin's pages.
    code, body = call(routes.frame_token, "settings", FakeReq(ST, same_origin(ST)))
    check("frame_token on Settings' origin mints Settings' token",
          code == 200 and tokens.verify(SECRET, body["token"]).app_id == "settings", f"{code} {body}")
    check("... bound to Settings' origin key",
          code == 200 and tokens.verify(SECRET, body["token"]).origin_key == K, f"{code} {body}")
    for label, app_id, host, extra, want in (
            ("on the desktop's origin", "settings", DESKTOP, same_origin(DESKTOP), 403),
            ("on Backups' origin", "settings", app_host("backups"), same_origin(app_host("backups")), 403),
            ("on the legacy shared origin", "settings", LEGACY, {}, 403),
            ("on a foreign zone's name for Settings' origin", "settings",
             f"apps-{K}-{H}.host.example.test", {}, 403),
            ("in-pod with no zone name", "settings", "127.0.0.1", {}, 403),
            ("from a desktop page addressing Settings' origin (same-site)", "settings", ST,
             {"sec-fetch-site": "same-site", "origin": f"http://{DESKTOP}:{PORT}"}, 403),
            ("for a same-origin app on its key's origin", "circles", app_host("circles"),
             same_origin(app_host("circles")), 403),
            ("for an app that is not installed", "nope", app_host("nope"), same_origin(app_host("nope")), 404)):
        code, detail = call(routes.frame_token, app_id, FakeReq(host, extra))
        check(f"frame_token refused {label} -> {want}", code == want, f"{code} {detail}")
    code, detail = call(routes.frame_token, "settings",
                        FakeReq(ST, same_origin(ST), token=tokens.verify(SECRET, bound_b)))
    check("frame_token refuses a caller holding another app's token", code == 403, f"{code} {detail}")
    code, body = call(routes.frame_token, "national-weather",
                      FakeReq(app_host("national-weather"), same_origin(app_host("national-weather"))))
    check("frame_token serves a community app on its own origin",
          code == 200 and tokens.verify(SECRET, body["token"]).origin_key == ao.origin_key("national-weather"),
          f"{code} {body}")

    # _serialize: the desktop learns each isolated app's key, and only theirs.
    # (The real manifest loader: these rows' source_dir does not exist, so
    # _serialize returns its base fields, which is where the key is.)
    routes.load_manifest = real[3]
    keys = run(routes._origin_keys(None))
    ser = {a: routes._serialize(APPS[a], keys) for a in APPS}
    check("an isolated app is serialised with its origin key",
          ser["settings"]["origin_key"] == K and ser["national-weather"]["origin_key"]
          == ao.origin_key("national-weather"))
    check("a same-origin app has no origin key",
          ser["circles"]["origin_key"] is None and ser["my-tool"]["origin_key"] is None)
    check("no key table, no key (fail closed)", routes._serialize(APPS["settings"])["origin_key"] is None)

    # 7. collisions — forced, since 100-bit keys never collide by chance.
    real_key = ao.origin_key
    try:
        ao.origin_key = lambda i: "aaaaaaaaaaaaaaaaaaaa" if i in ("settings", "national-weather",
                                                                  "evil") else real_key(i)
        ranked = ao.keys_for([APPS["national-weather"], APPS["settings"]])
        check("on a collision the image-baked app keeps the origin, whatever the order",
              ranked == {"settings": "aaaaaaaaaaaaaaaaaaaa", "national-weather": None}, ranked)
        # A first-party app that arrives in a LATER image than a community app
        # already holding its key still wins it: an earlier install is no claim
        # against an image-baked app.
        first, later_native = row("national-weather", "community", 0), row("settings", "native", 9)
        ranked = ao.keys_for([first, later_native])
        check("an image-baked app installed later still beats an earlier community app",
              ranked == {"settings": "aaaaaaaaaaaaaaaaaaaa", "national-weather": None}, ranked)
        early, late = row("evil", "community", 1), row("national-weather", "community", 9)
        check("between two community apps the earlier install keeps it",
              ao.keys_for([late, early]) == {"evil": "aaaaaaaaaaaaaaaaaaaa", "national-weather": None})
        code, detail = call(routes.frame_token, "national-weather",
                            FakeReq(app_host("national-weather"), same_origin(app_host("national-weather"))))
        check("frame_token refuses the app that lost its origin", code == 409, f"{code} {detail}")

        from server.applications import installer

        class Conn:
            async def fetch(self, _q, app_id):
                return [{"id": i} for i in APPS if i != app_id]

        class Pool:
            def acquire(self):
                class _Ctx:
                    async def __aenter__(self_inner):
                        return Conn()

                    async def __aexit__(self_inner, *a):
                        return False
                return _Ctx()

        real_baked = installer.image_baked_ids
        installer.image_baked_ids = lambda: frozenset({"settings"})
        try:
            try:
                run(installer._refuse_origin_key_collision(Pool(), "evil"))
                refused = False
            except installer.InstallError:
                refused = True
            check("the installer refuses a community app whose key another app holds", refused)
            # The app's own id, installed and baked, is not a clash with itself.
            ao.origin_key = lambda i: "bbbbbbbbbbbbbbbbbbbb" if i == "settings" else real_key(i)
            try:
                run(installer._refuse_origin_key_collision(Pool(), "settings"))
                own = True
            except installer.InstallError:
                own = False
            check("... and an upgrade of the app itself is not a collision", own)
        finally:
            installer.image_baked_ids = real_baked
    finally:
        ao.origin_key = real_key
finally:
    registry.get, registry.list_active, brain_service._pool, routes.load_manifest = real

inst_src = (ROOT / "server" / "applications" / "installer.py").read_text()
check("install_bundle runs the collision refusal for community apps",
      'if manifest.provenance == "community":\n        await _refuse_origin_key_collision(pool, manifest.id)'
      in inst_src)

# ── 8. the desktop's half, run in node ───────────────────────────────────
node = shutil.which("node")
check("node is available for the desktop half", node is not None)
if node:
    harness = r"""
const fs = require('fs');
global.window = { addEventListener() {}, location: {} };
global.document = { addEventListener() {} };
eval(fs.readFileSync(process.argv[1], 'utf8') + '\nglobal.__AR = AppRunner;');
const cases = JSON.parse(process.argv[2]);
console.log(JSON.stringify(cases.map(c => global.__AR.frameOrigin(c.m, c.loc))));
"""
    # location.host carries the port, location.protocol the scheme: the frame's
    # origin takes both from the page, so a desktop on http :8800 frames its
    # apps on http :8800 and one behind a TLS proxy (https, no port) on https.
    HOME_LOC = {"protocol": "http:", "host": f"{DESKTOP}:{PORT}"}
    cases = [
        {"m": {"isolated": True, "origin_key": K, "id": "settings"}, "loc": HOME_LOC},
        {"m": {"isolated": True, "origin_key": K, "id": "settings"},
         "loc": {"protocol": "https:", "host": DESKTOP}},
        {"m": {"isolated": True, "origin_key": None, "id": "settings"}, "loc": HOME_LOC},
        {"m": {"isolated": True, "origin_key": K[:19] + "1", "id": "settings"}, "loc": HOME_LOC},
        {"m": {"isolated": False, "origin_key": K, "id": "circles"}, "loc": HOME_LOC},
        {"m": {"isolated": True, "origin_key": K, "id": "settings"},
         "loc": {"protocol": "http:", "host": ("d" * 38) + f"{ZONE}:{PORT}"}},
    ]
    out = subprocess.run([node, "-e", harness, str(ROOT / "dashboard/base/js/app-runner.js"),
                          json.dumps(cases)], capture_output=True, text=True, timeout=30)
    try:
        got = json.loads(out.stdout.strip().splitlines()[-1])
    except Exception:
        got = None
        print(out.stdout, out.stderr)
    check("app-runner builds the same host the kernel computes, on the desktop's scheme and port",
          got and got[0] == f"http://{ao.app_host('settings', DESKTOP)}:{PORT}", repr(got and got[0]))
    check("... and with no port when the page has none (a TLS proxy in front)",
          got and got[1] == "https://" + ao.app_host("settings", DESKTOP), repr(got and got[1]))
    check("... and refuses to frame with no key, a bad key, a same-origin app, or no room",
          got and got[2:] == [None, None, None, None], repr(got and got[2:]))
    runner = (ROOT / "dashboard/base/js/app-runner.js").read_text()
    check("app-runner no longer builds the shared apps-<hostname> origin",
          "'apps-' + window.location.hostname" not in runner)
    check("app-runner hands no token to an isolated frame (its frame fetches its own)",
          "if (sdkManaged && !isolated)" in runner)

print()
if failures:
    print(f"FAIL: {len(failures)} app-origin check(s) failed")
    sys.exit(1)
print("PASS app-origin (one origin per isolated app)")
