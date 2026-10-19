#!/usr/bin/env python3
"""Gate: only the person's own input keeps a Host session, and a desktop whose
session ended stops sending requests.

2026-10-06: a Host session ends after an hour with no real use, and
active use is never cut, up to 24h. The router's use clock moves only on
`/__host/renew?use=1` sent same-origin to a desktop (the direct router's
"THE USE CLOCK"). This drives the real
dashboard/base/js/session-keeper.js and app-frame-init.js in Chromium and
checks what they send:

  U. the person's input sends `use=1`, same-origin, at most once a minute for
     the browser (a second desktop tab shares the limit): a key press on the
     desktop; a click in a same-origin app frame; a click in an ISOLATED app
     frame on another origin (the browser's userActivation, which no script or
     message can set); switching back to the desktop tab;
  N. nothing else does: an hour of 5-minute pings, a synthetic (script-made)
     key or pointer event, a failed request, an app frame's messages, the tab
     shown without the window's focus — and the isolated app never sends
     `use=1` itself;
  R. when that input's report is the one that finds the session ended, a
     tab-wake after the session is back still asks, and the prompt comes down;
  S. once a renewal says 401, the desktop's poll, a same-origin frame's poll
     (one that reloaded while the desktop was in a background tab) and the
     isolated app's poll stop reaching the server; when the session is back
     (a ping answers 204) they resume;
  W. the shell's WebSocket (ws-client.js) stops reconnecting once the session
     is known ended, and reconnects at once when it is back;
  WB. so does the BUILT Workbench's own delivery socket, and a 'session back'
     message that is not the desktop's does not bring it back.

Fail-before: APP_ROOT=<a tree without the change> python3 <this file> fails
every U and S check.

Needs Playwright + Chromium and free local ports — no pod, no DB. Run:

  APP_ROOT=<repo> python3 scripts/test_session_real_use.py
"""
from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import socket
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
JS = ROOT / "dashboard" / "base" / "js"

fails = 0


def check(name, ok, detail=""):
    global fails
    print(("  ok    " if ok else "  FAIL  ") + name + (f"  — {detail}" if detail and not ok else ""))
    if not ok:
        fails += 1


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))       # loopback on purpose: the only client is this process's Chromium
        return s.getsockname()[1]


DESK_PORT, APP_PORT, SIGNIN_PORT = free_port(), free_port(), free_port()
CMD_ORIGIN = f"http://127.0.0.1:{SIGNIN_PORT}"
DESK = f"http://127.0.0.1:{DESK_PORT}"          # the desktop origin
APP = f"http://localhost:{APP_PORT}"            # an isolated app's own origin
STATE = {"alive": True}
LOG: list = []                                   # (origin, path, query, sec-fetch-site)
LOCK = threading.Lock()

# Every page runs the commands the test queues for it (CMD_ORIGIN, another
# origin with CORS, so the keeper's same-origin handling never touches it). The
# test drives no page through Playwright's evaluate: Playwright evaluates with a
# user gesture, which would hand the page a transient user activation — the
# very signal under test. Input is sent only as real input (page.mouse /
# page.keyboard, CDP Input events).
CMD_JS = """
(function (who) {
  function run(c) {
    if (c === 'polls') setInterval(function () { fetch(POLL).then(function () {}, function () {}); }, 1000);
    if (c === 'synthetic') {
      document.dispatchEvent(new KeyboardEvent('keydown', {key: 'a', bubbles: true}));
      document.body.dispatchEvent(new PointerEvent('pointerdown', {bubbles: true}));
      window.dispatchEvent(new FocusEvent('focus'));
    }
    if (c === 'check') parent.postMessage({type: 'cseries:session-check'}, '*');
    if (c === 'junk') { parent.postMessage({type: 'cseries:use'}, '*'); parent.postMessage('use', '*'); }
    if (c === 'fail') fetch('/api/redirect-out').then(function () {}, function () {});
    if (c === 'report') fetch(CMD + '/report?who=' + who + '&overlay=' +
        (document.getElementById('session-expired-overlay') ? 1 : 0)).then(function () {}, function () {});
    if (c === 'wake') document.dispatchEvent(new Event('visibilitychange'));
    if (c === 'reload') location.reload();
    // The tab's visibility and the window's focus, stubbed: headless Chromium
    // keeps every page visible and focused (measured: bring_to_front changes
    // neither, focus emulation on or off), so a tab switch cannot be made for
    // real. Only the browser's state is stubbed; the keeper decides.
    if (c === 'away') view(false, false);
    if (c === 'back') view(true, true);
    if (c === 'shown') view(true, false);
    if (c === 'restore') { delete document.visibilityState; delete document.hasFocus; }
  }
  function view(vis, focus) {
    Object.defineProperty(document, 'visibilityState',
      {get: function () { return vis ? 'visible' : 'hidden'; }, configurable: true});
    document.hasFocus = function () { return focus; };
    document.dispatchEvent(new Event('visibilitychange'));
  }
  setInterval(function () {
    fetch(CMD + '/cmd?who=' + who).then(function (r) { return r.json(); })
      .then(function (list) { list.forEach(run); }, function () {});
  }, 250);
})
"""


def page_js(who, poll):
    return (f"<script>var CMD = '{CMD_ORIGIN}', POLL = '{poll}';" + CMD_JS + f"('{who}');</script>")


DESKTOP_HTML = f"""<!doctype html><html><head><meta charset="utf-8">
<script src="/js/session-keeper.js"></script></head><body>
<input id="desk-input" style="width:200px">
<iframe id="same" src="/same.html" style="width:300px;height:120px;border:0"></iframe>
<iframe id="iso" src="{APP}/app.html" style="width:300px;height:120px;border:0"></iframe>
{page_js('desk', '/api/poll')}</body></html>"""

SAME_HTML = f"""<!doctype html><html><body style="margin:0"><input id="same-input" style="width:200px">
{page_js('same', '/api/same-poll')}</body></html>"""

# A second desktop tab of the same Host: the keeper and somewhere to click.
DESKTOP2_HTML = """<!doctype html><html><head><meta charset="utf-8">
<script src="/js/session-keeper.js"></script></head><body style="margin:0;height:300px"></body></html>"""

APP_HTML = f"""<!doctype html><html><head><meta charset="utf-8">
<script src="/js/app-frame-init.js" data-app-id="probe-app"></script></head><body style="margin:0">
<input id="app-input" style="width:200px">
{page_js('app', '/api/app-poll')}</body></html>"""

# A desktop tab that also runs the shell's WebSocket client (ws-client.js).
DESKTOP_WS_HTML = f"""<!doctype html><html><head><meta charset="utf-8">
<script src="/js/session-keeper.js"></script><script src="/js/ws-client.js"></script></head><body>
{page_js('ws', '/api/ws-poll')}</body></html>"""
# A desktop tab framing the real BUILT Workbench (system-apps/workbench), which
# holds its own delivery socket (workbench-web/src/hooks/use-delivery-socket.ts).
DESKTOP_WB_HTML = f"""<!doctype html><html><head><meta charset="utf-8">
<script src="/js/session-keeper.js"></script></head><body style="margin:0">
<iframe id="wb" src="/system-apps/workbench/index.html" style="width:1200px;height:700px;border:0"></iframe>
{page_js('wb', '/api/wb-desk-poll')}</body></html>"""
WB_DIR = ROOT / "system-apps" / "workbench"
# What the Workbench reads as it opens, answered empty but in shape (the set
# scripts/test_session_ended_first_failure.py mocks); anything else is a 404.
WB_ROUTES = {
    "/api/projects": {"projects": []},
    "/api/projects/buckets": {"buckets": [], "unfiled": {"counts": {}}},
    "/api/compute/runs": {"runs": []},
    "/api/review/sessions": {"sessions": []},
    "/api/applications": {"applications": [{"id": "workbench", "name": "Workbench"}]},
    "/api/agents": {"agents": []},
    "/api/agents/scheduled": {"scheduled": []},
    "/api/brain/chat": {"turns": []},
}
CTYPES = {".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml",
          ".png": "image/png", ".woff2": "font/woff2", ".json": "application/json"}
BOOT_AT = time.time()                            # the mock pod's boot instant (/api/system/status)
WS_CLOSE = threading.Event()                     # set: every open socket is closed by the server

CMDS = {"desk": [], "same": [], "app": [], "ws": [], "wb": []}
REPORTS: list = []


def send(who, cmd):
    with LOCK:
        CMDS[who].append(cmd)


# The keeper's own rhythms, shortened for a test that runs in real time (a fake
# clock is out: Playwright drives it through evaluate, which grants a user
# activation). Only the numbers change; every replacement must land.
SHORT = {
    "session-keeper.js": [("const INTERVAL_MS = 5 * 60 * 1000;", "const INTERVAL_MS = 6000;"),
                          ("const USE_REPORT_MS = 60 * 1000;", "const USE_REPORT_MS = 3000;")],
    "app-frame-init.js": [("var INTERVAL_MS = 5 * 60 * 1000;", "var INTERVAL_MS = 6000;")],
    "ws-client.js": [("const MAX_RECONNECT_DELAY = 30000;", "const MAX_RECONNECT_DELAY = 1000;")],
}
SHORT_MISSED: list = []


def shorten(name, src):
    for a, b in SHORT.get(name, []):
        if a in src:
            src = src.replace(a, b, 1)
        else:
            SHORT_MISSED.append((name, a))
    return src


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code, body=b"", ctype="text/plain", extra=()):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        if body and self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass                     # the page went away mid-answer (a tab closed)

    def do_POST(self):
        self.do_GET()

    def do_GET(self):
        u = urlsplit(self.path)
        origin = f"http://{self.headers.get('Host')}"
        if u.path == "/cmd":
            who = u.query.split("who=", 1)[-1]
            with LOCK:
                out, CMDS[who][:] = list(CMDS.get(who, [])), []
            return self._send(200, json.dumps(out).encode(), "application/json",
                              extra=(("Access-Control-Allow-Origin", "*"),))
        if u.path == "/report":
            with LOCK:
                REPORTS.append(u.query)
            return self._send(204, extra=(("Access-Control-Allow-Origin", "*"),))
        with LOCK:
            LOG.append((origin, u.path, u.query, self.headers.get("Sec-Fetch-Site"), time.time()))
        if u.path == "/__host/renew":
            return self._send(204 if STATE["alive"] else 401)
        if u.path == "/__host/session":
            return self._send(302 if STATE["alive"] else 401,
                              extra=(("Location", "/__host/renew"),) if STATE["alive"] else ())
        if u.path.startswith("/js/"):
            f = JS / u.path[len("/js/"):]
            if not f.is_file():
                return self._send(404)
            return self._send(200, shorten(f.name, f.read_text()).encode(), "application/javascript")
        if u.path == "/" or u.path == "/desktop.html":
            return self._send(200, DESKTOP_HTML.encode(), "text/html")
        if u.path == "/ws":
            # The router refuses the upgrade once the session ended; while it
            # lives this is a WebSocket the server holds until WS_CLOSE.
            if not STATE["alive"]:
                return self._send(401, b"session expired\n")
            key = (self.headers.get("Sec-WebSocket-Key") or "").strip()
            accept = base64.b64encode(hashlib.sha1(
                (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
            self.send_response(101)
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.send_header("Sec-WebSocket-Accept", accept)
            self.end_headers()
            self.close_connection = True
            while not WS_CLOSE.wait(0.1):
                pass
            try:
                self.wfile.write(b"\x88\x00")     # a close frame, no payload
                self.wfile.flush()
            except OSError:
                pass
            return
        if u.path == "/desktop-ws.html":
            return self._send(200, DESKTOP_WS_HTML.encode(), "text/html")
        if u.path == "/desktop-wb.html":
            return self._send(200, DESKTOP_WB_HTML.encode(), "text/html")
        if u.path.startswith("/system-apps/workbench/"):
            f = (WB_DIR / u.path[len("/system-apps/workbench/"):]).resolve()
            if WB_DIR.resolve() not in f.parents or not f.is_file():
                return self._send(404)
            return self._send(200, f.read_bytes(), CTYPES.get(f.suffix, "application/octet-stream"))
        if u.path == "/desktop2.html":
            return self._send(200, DESKTOP2_HTML.encode(), "text/html")
        if u.path == "/same.html":
            return self._send(200, SAME_HTML.encode(), "text/html")
        if u.path == "/app.html":
            return self._send(200, APP_HTML.encode(), "text/html")
        if u.path.startswith("/api/applications/"):
            return self._send(200, b'{"token":"t"}', "application/json")
        if u.path == "/api/redirect-out" or (u.path.startswith("/api/") and not STATE["alive"]):
            # what the router does with an ended session: off to the sign-in, another origin
            return self._send(302, extra=(("Location", f"{CMD_ORIGIN}/signin"),))
        if u.path == "/api/system/status":
            # one boot instant throughout, so ws-client sees no pod restart on a reconnect
            now = time.time()
            body = json.dumps({"timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
                               "uptime_s": int(now - BOOT_AT)}).encode()
            return self._send(200, body, "application/json")
        if u.path in WB_ROUTES:
            return self._send(200, json.dumps(WB_ROUTES[u.path]).encode(), "application/json")
        if u.path.startswith("/api/"):
            return self._send(404, b'{"detail": "not mocked"}', "application/json")
        return self._send(404)

    def log_message(self, *a):
        pass


for port in (DESK_PORT, APP_PORT, SIGNIN_PORT):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)   # loopback on purpose (as above)
    threading.Thread(target=srv.serve_forever, daemon=True).start()


def since(mark):
    with LOCK:
        return LOG[mark:]


def mark():
    with LOCK:
        return len(LOG)


def uses(entries, origin=DESK):
    return [e for e in entries if e[0] == origin and e[1] == "/__host/renew" and "use=1" in (e[2] or "")]


def plain_renews(entries, origin=DESK):
    return [e for e in entries if e[0] == origin and e[1] == "/__host/renew" and "use=1" not in (e[2] or "")]


def wait_for(pred, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.1)
    return pred()


with sync_playwright() as pw:
    browser = pw.chromium.launch()
    ctx = browser.new_context(viewport={"width": 1000, "height": 400})   # one context: tabs share storage
    page = ctx.new_page()
    page.goto(DESK + "/desktop.html")
    page.wait_for_load_state("load")
    # Where to click, read once now (bounding_box reads layout, not script).
    desk_box = page.locator("#desk-input").bounding_box()
    same_box = page.locator("#same").bounding_box()
    iso_box = page.locator("#iso").bounding_box()
    # Reading layout through Playwright is an evaluate, so it hands the page a
    # transient activation: let that lapse (a few seconds of real time) and
    # one report interval pass before anything is measured.
    time.sleep(9)
    check("setup: the shortened rhythms applied", not SHORT_MISSED, str(SHORT_MISSED))

    # ── N. nothing but the person's input reports use ──
    t0 = mark()
    time.sleep(14)                       # >= two 6-second pings, no input at all
    e = since(t0)
    check("N 14s with no input: the pings went out", len(plain_renews(e)) >= 2, f"{len(plain_renews(e))}")
    check("N ... and not one of them reports use", not uses(e), str(uses(e)[:2]))

    m = mark()
    send("desk", "synthetic")
    send("same", "synthetic")
    send("app", "synthetic")
    time.sleep(4)
    check("N script-made key, pointer and focus events report no use", not uses(since(m)), str(uses(since(m))))

    m = mark()
    send("app", "check")
    send("app", "junk")
    time.sleep(4)
    e = since(m)
    check("N an isolated app's messages report no use", not uses(e), str(uses(e)))
    check("N ... though its session-check made the desktop ask (plainly)", len(plain_renews(e)) >= 1,
          f"{len(plain_renews(e))}")

    m = mark()
    send("desk", "fail")
    time.sleep(4)
    e = since(m)
    check("N a failed request makes the desktop ask (plainly), and reports no use",
          not uses(e) and len(plain_renews(e)) >= 1, f"uses={len(uses(e))} plain={len(plain_renews(e))}")

    # ── U. the person's own input ──
    def act_then(what, action, origin=DESK):
        # The browser's record of the last input lapses after a few seconds,
        # and the report interval is 3s here: wait out both.
        time.sleep(9)
        m = mark()
        action()
        ok = wait_for(lambda: len(uses(since(m), origin)) >= 1, 3.0)
        e = since(m)
        check(f"U {what} reports use", ok and len(uses(e, origin)) == 1, f"{len(uses(e, origin))} reports")
        return e

    e = act_then("a click on the desktop", lambda: page.mouse.click(desk_box["x"] + 20, desk_box["y"] + 8))
    check("U ... sent same-origin (what the router requires)",
          bool(uses(e)) and all(x[3] == "same-origin" for x in uses(e)), str([x[3] for x in uses(e)]))
    act_then("a key press on the desktop", lambda: page.keyboard.press("a"))
    m = mark()
    for _ in range(4):
        page.keyboard.press("b")
        time.sleep(0.4)
    time.sleep(0.5)
    check("U steady typing inside the report interval: one report at most",
          len(uses(since(m))) <= 1, str(len(uses(since(m)))))
    act_then("a click in a same-origin app frame",
             lambda: page.mouse.click(same_box["x"] + 20, same_box["y"] + 8))
    act_then("typing in a same-origin app frame", lambda: page.keyboard.type("hi"))
    act_then("a click in an ISOLATED app frame (another origin), seen by the desktop",
             lambda: page.mouse.click(iso_box["x"] + 20, iso_box["y"] + 8))
    act_then("typing in the isolated app frame, seen by the desktop", lambda: page.keyboard.type("hi"))
    check("U the isolated app itself never sends use", not uses(LOG, APP), str(uses(LOG, APP)[:2]))

    # One report per interval for the BROWSER: a second desktop tab of the same
    # Host shares the limit (input is real input there too: CDP to that tab).
    tab2 = ctx.new_page()
    tab2.goto(DESK + "/desktop2.html")
    tab2.wait_for_load_state("load")
    time.sleep(9)
    m = mark()
    page.mouse.click(desk_box["x"] + 20, desk_box["y"] + 8)
    wait_for(lambda: len(uses(since(m))) >= 1, 3.0)
    tab2.mouse.click(30, 30)
    tab2.keyboard.press("a")
    time.sleep(1.5)
    check("U input in a second desktop tab inside the report interval: still one report",
          len(uses(since(m))) == 1, f"{len(uses(since(m)))} reports")
    time.sleep(3.5)
    m = mark()
    tab2.mouse.click(30, 30)
    ok = wait_for(lambda: len(uses(since(m))) >= 1, 3.0)
    check("U ... and once the interval has passed, that tab's input reports", ok and len(uses(since(m))) == 1,
          f"{len(uses(since(m)))} reports")
    tab2.close()

    # The person switching back to the desktop tab is the window coming to the
    # front; the tab being shown without the window's focus is not.
    time.sleep(9)
    m = mark()
    send("desk", "away")
    time.sleep(1.5)
    send("desk", "back")
    ok = wait_for(lambda: len(uses(since(m))) >= 1, 3.0)
    check("U switching back to the desktop tab (hidden, then shown with focus) reports use",
          ok and len(uses(since(m))) == 1, f"{len(uses(since(m)))} reports")
    time.sleep(4)
    m = mark()
    send("desk", "away")
    time.sleep(1.5)
    send("desk", "shown")
    time.sleep(3)
    check("N the tab shown again without the window's focus (visibilitychange alone) reports no use",
          not uses(since(m)), str(uses(since(m))))
    send("desk", "restore")

    # ── R. input as the session ends, then the session is back and a tab-wake asks ──
    # A refused request leaves a probe waiting out the 2 s gap; the person's
    # input reports use meanwhile and that report's 401 ends the session. A
    # tab-wake that joins the waiting probe must still ask the router — it was
    # dropped, and the prompt stayed up over a live session (2026-10-07).
    # Started just after a 6 s ping, so no ping lands in the window measured.
    time.sleep(9)
    m = mark()
    wait_for(lambda: len(plain_renews(since(m))) >= 1, 7.0)
    time.sleep(0.2)
    STATE["alive"] = False
    m = mark()
    send("desk", "fail")
    wait_for(lambda: any(x[1] == "/api/redirect-out" for x in since(m)), 2.0)
    m = mark()
    page.mouse.click(desk_box["x"] + 20, desk_box["y"] + 8)
    check("R setup: the person's input right after a refused request reports use",
          wait_for(lambda: len(uses(since(m))) >= 1, 1.5))
    STATE["alive"] = True
    m = mark()
    send("desk", "wake")
    asked = wait_for(lambda: len(plain_renews(since(m))) >= 1, 2.8)
    check("R a tab-wake while a probe waits out the gap still asks the router", asked,
          "no renewal after the tab-wake")
    REPORTS.clear()
    send("desk", "report")
    wait_for(lambda: REPORTS, 3)
    check("R ... and the prompt comes down", any("overlay=0" in r for r in REPORTS), str(REPORTS))
    time.sleep(4)

    # ── S. a session known ended stops the polls ──
    # On a desktop in a BACKGROUND tab (where a lapsed one sits), whose
    # same-origin app frame reloaded there: a new page in the same frame.
    send("desk", "away")
    send("same", "reload")
    time.sleep(2.5)
    for who in ("desk", "same", "app"):
        send(who, "polls")
    time.sleep(2.5)
    check("S setup: all three polls reach the server while the session lives",
          all(any(x[1] == p for x in since(mark() - 60)) for p in ("/api/poll", "/api/same-poll", "/api/app-poll")))
    STATE["alive"] = False
    m0 = mark()
    time.sleep(4)                        # first failures -> the keeper asks -> 401
    m = mark()
    time.sleep(8)
    e = since(m)
    desk_polls = [x for x in e if x[1] == "/api/poll"]
    same_polls = [x for x in e if x[1] == "/api/same-poll"]
    app_polls = [x for x in e if x[1] == "/api/app-poll"]
    REPORTS.clear()
    send("desk", "report")
    wait_for(lambda: REPORTS, 3)
    check("S the sign-in prompt is up once a renewal said 401",
          any("overlay=1" in r for r in REPORTS), str(REPORTS))
    check("S after that, the desktop's poll sends nothing", not desk_polls, f"{len(desk_polls)} in 8s")
    check("S ... nor the same-origin app frame's poll", not same_polls, f"{len(same_polls)} in 8s")
    check("S ... nor the isolated app's poll", not app_polls, f"{len(app_polls)} in 8s")
    sent = [x for x in since(m0) if x[1].startswith("/api/")]
    check("S app requests in the 12s after the end: only the first few", len(sent) <= 12, f"{len(sent)}")

    STATE["alive"] = True
    m = mark()
    ok = wait_for(lambda: any(x[1] == "/api/poll" for x in since(m)), 10)
    check("S when the session is back (a ping answers 204), the desktop's poll resumes", ok)
    ok = wait_for(lambda: any(x[1] == "/api/app-poll" for x in since(m)), 10)
    check("S ... and the isolated app's poll resumes", ok)
    REPORTS.clear()
    send("desk", "report")
    wait_for(lambda: REPORTS, 3)
    check("S ... and the prompt comes down", any("overlay=0" in r for r in REPORTS), str(REPORTS))

    # ── W. the shell's WebSocket waits for the session ──
    # Its reconnect backs off to 30 s and never stopped: a lapsed desktop kept
    # knocking for as long as the tab stayed open. The cap is 1 s here, so a
    # reconnect that does not stop shows as several attempts a window.
    def overlay_is(who, want, timeout=6.0):
        end = time.time() + timeout
        while time.time() < end:
            REPORTS.clear()
            send(who, "report")
            if wait_for(lambda: REPORTS, 1.5) and any(f"overlay={want}" in r for r in REPORTS):
                return True
            time.sleep(0.2)
        return False

    STATE["alive"] = True
    tab3 = ctx.new_page()
    m = mark()
    tab3.goto(DESK + "/desktop-ws.html")
    tab3.wait_for_load_state("load")
    check("W setup: the desktop's socket connects while the session lives",
          wait_for(lambda: any(x[1] == "/ws" for x in since(m)), 5.0))
    check("W setup: ws-client's shortened reconnect cap applied", not SHORT_MISSED, str(SHORT_MISSED))
    time.sleep(1)
    STATE["alive"] = False
    WS_CLOSE.set()                       # the socket drops as the session ends
    time.sleep(0.3)
    WS_CLOSE.clear()
    send("ws", "fail")                   # the desktop's next request is refused: its keeper asks
    check("W setup: the keeper knows the session ended (the prompt is up)", overlay_is("ws", 1))
    m = mark()
    time.sleep(8)
    tries = [x for x in since(m) if x[1] == "/ws"]
    check("W once the session is known ended, the socket stops reconnecting", not tries,
          f"{len(tries)} attempts in 8s")
    STATE["alive"] = True
    m = mark()
    send("ws", "wake")
    check("W when the session is back (a tab-wake answers 204), the socket reconnects at once",
          wait_for(lambda: any(x[1] == "/ws" for x in since(m)), 4.0))
    check("W ... and the prompt is down", overlay_is("ws", 0))
    tab3.close()

    # ── WB. the Workbench's own delivery socket waits for the session ──
    # It retried every 5 s for as long as the Workbench stayed open. Over a
    # 16 s window that is three attempts; once the session is known ended
    # there must be none, and the desktop's 'session back' brings it back.
    STATE["alive"] = True
    tab4 = ctx.new_page()
    m = mark()
    tab4.goto(DESK + "/desktop-wb.html")
    tab4.wait_for_load_state("load")
    check("WB setup: the Workbench's socket connects while the session lives",
          wait_for(lambda: any(x[1] == "/ws" for x in since(m)), 10.0))
    time.sleep(1)
    STATE["alive"] = False
    WS_CLOSE.set()                       # the socket drops as the session ends
    time.sleep(0.3)
    WS_CLOSE.clear()
    check("WB setup: the keeper knows the session ended (the prompt is up)", overlay_is("wb", 1, 8.0))
    m = mark()
    time.sleep(16)
    tries = [x for x in since(m) if x[1] == "/ws"]
    check("WB once the session is known ended, the Workbench's socket stops reconnecting", not tries,
          f"{len(tries)} attempts in 16s")
    # A 'session back' that is not the desktop's: the Workbench's own frame
    # posts it to itself (any frame that can reach it could).
    wb_frame = next(f for f in tab4.frames if "/system-apps/workbench/" in f.url)
    m = mark()
    wb_frame.evaluate("() => window.postMessage({ type: 'cseries:session', state: 'ok' }, '*')")
    time.sleep(3)
    forged = [x for x in since(m) if x[1] == "/ws"]
    check("WB ... and a 'session back' that is not the desktop's does not reconnect it", not forged,
          f"{len(forged)} attempts")
    STATE["alive"] = True
    m = mark()
    send("wb", "wake")
    check("WB when the session is back (the desktop announces it), the Workbench's socket reconnects",
          wait_for(lambda: any(x[1] == "/ws" for x in since(m)), 4.0))
    tab4.close()
    browser.close()

print(f"\n{'ALL PASS' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
