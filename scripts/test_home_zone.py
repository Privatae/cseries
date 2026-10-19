#!/usr/bin/env python3
"""End to end, in real browsers: an origin-isolated app loads at home.

Boots C-Series from compose.yaml under its own project, opens the desktop on
its home address (http://home.cseries.localhost:<port>, which the browser
itself sends to this machine), creates the owner, and frames an isolated app
from its own origin the way the desktop does (app-runner.js frameOrigin). Then:

  * the frame's first load goes through the desktop's /__host/join and comes
    back with a session of the app origin's own (a different cookie value);
  * a response on the app origin lets the desktop, on its scheme and port,
    frame it;
  * the frame gets its window token (frame-token) on its own origin;
  * the app origin's session is refused on the desktop, and the desktop's on
    the app origin;
  * a page on the app origin cannot pass as the desktop with its own
    X-Forwarded-Host;
  * /__host/join sends a code to no origin but this Host's own apps;
  * a join code works once, only on the host it was made for;
  * signing out on the desktop ends the app origin's session too, and the
    desktop's own Continue (/__host/session) then lands on the sign-in page.

Run with the Playwright Python (default /opt/playwright-venv/bin/python3):

  COMPOSE=/path/to/docker-compose scripts/test_home_zone.py

Each browser in BROWSERS (default "chromium firefox") runs the browser checks.
Needs docker and images built from this tree (BUILD=1 builds them).
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PROJECT = f"cseries-zone-test-{os.getpid()}"
PORT = int(os.environ.get("CSERIES_PORT", "18880"))
HOME = f"http://home.cseries.localhost:{PORT}"
EMAIL, PASSWORD = "owner@example.com", "correct horse battery"
BROWSERS = os.environ.get("BROWSERS", "chromium firefox").split()
COMPOSE = shlex.split(os.environ.get("COMPOSE", "docker compose"))

failed = 0


def check(cond, what, detail=""):
    global failed
    print(("OK    " if cond else "FAIL  ") + what + ("" if cond else f"  <- {str(detail)[:400]}"))
    if not cond:
        failed += 1


def dc(*args, capture=True, input_=None):
    # The Host's two bastion pairing ports, away from 8810/8811 like 8800 is.
    env = {"CSERIES_ENROLL_PORT": str(PORT + 2), "CSERIES_LANE_PORT": str(PORT + 3),
           **os.environ, "CSERIES_PORT": str(PORT)}
    env.pop("CSERIES_URL", None)
    return subprocess.run([*COMPOSE, "-f", str(REPO / "compose.yaml"), "-p", PROJECT, *args],
                          capture_output=capture, text=True, env=env, input=input_)


def in_host_python(code: str) -> str:
    """Run `code` with the Host's own Python and modules, return its last line."""
    r = dc("exec", "-T", "-w", "/app", "host", "python3", "-", input_=code)
    return (r.stdout.strip().splitlines() or [""])[-1] + (f" {r.stderr[-300:]}" if r.returncode else "")


def wait_healthy(deadline_s=240):
    end = time.time() + deadline_s
    while time.time() < end:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/system/health", timeout=2)
            return True
        except (urllib.error.URLError, OSError):
            time.sleep(2)
    return False


def setup_code() -> str:
    m = re.search(r"CSERIES SETUP CODE: ([0-9a-f-]+)", dc("logs", "--no-color", "host").stdout)
    return m.group(1) if m else ""


JOIN_UNIT = r'''
import asyncio, sys
sys.path[:0] = ["/app", "/app/brain"]
from server import brain_service, owner_login as O
async def main():
    import asyncpg
    pool = await asyncpg.create_pool(host="/var/run/postgresql", user="entity", database="hostmem",
                                     min_size=1, max_size=1)
    brain_service._pool = pool
    await O.init()
    out = []
    desk = await O.sign_in("%(email)s", "%(password)s", "unit", "home.cseries.localhost")
    app = "apps-aaaaaaaaaaaaaaaaaaaa-home.cseries.localhost"
    code = O.start_join(desk, app)
    out.append(await O.finish_join(code, "apps-bbbbbbbbbbbbbbbbbbbb-home.cseries.localhost") is None)
    code = O.start_join(desk, app)
    child = await O.finish_join(code, app)
    out.append(child is not None)
    out.append(await O.finish_join(code, app) is None)                      # once only
    out.append(await O.session_email(child, app) == "%(email)s")
    out.append(await O.session_email(child, "home.cseries.localhost") is None)
    out.append(await O.session_email(desk, app) is None)
    code = O.start_join(desk, app)
    O._joins[O._token_hash(code)] = (*O._joins[O._token_hash(code)][:2], 0.0)   # expired
    out.append(await O.finish_join(code, app) is None)
    code = O.start_join(child, app)                                          # a child cannot join
    out.append(await O.finish_join(code, app) is None)
    O._cache.clear()
    await O.sign_out(desk)
    out.append(await O.session_email(child, app) is None)
    n = await pool.fetchval("SELECT count(*) FROM owner_sessions WHERE token_hash = $1", O._token_hash(child))
    out.append(n == 0)
    print(",".join("1" if x else "0" for x in out))
asyncio.run(main())
'''

JOIN_LABELS = [
    "a join code is refused on another app's host",
    "a join code made for an app host gives that host a session",
    "a join code works once",
    "the app host's session is live on that host",
    "...and refused on the desktop",
    "the desktop's session is refused on the app host",
    "an expired join code is refused",
    "an app host's session cannot make a join of its own",
    "signing out the desktop session ends the app host's",
    "...and removes its row",
]


def browser_checks(pw, name: str, app_id: str, key: str):
    app_host = f"apps-{key}-home.cseries.localhost"
    app_origin = f"http://{app_host}:{PORT}"
    browser = getattr(pw, name).launch()
    ctx = browser.new_context()
    page = ctx.new_page()
    try:
        page.goto(f"{HOME}/login", wait_until="domcontentloaded")
        page.fill("#email", EMAIL)
        page.fill("#password", PASSWORD)
        page.click("button[type=submit]")
        page.wait_for_url(f"{HOME}/", timeout=15000)
        check(page.url == f"{HOME}/", f"[{name}] signed in on the home address", page.url)

        # Frame the app the way app-runner.js does.
        page.evaluate("""(src) => { const f = document.createElement('iframe');
            f.id = 'zone-test'; f.src = src; document.body.appendChild(f); }""",
                      f"{app_origin}/system-apps/{app_id}/?v=1")
        frame = None
        end = time.time() + 20
        while time.time() < end and frame is None:
            frame = next((f for f in page.frames if f.url.startswith(app_origin + f"/system-apps/{app_id}/")),
                         None)
            if frame is None:
                page.wait_for_timeout(250)   # lets Playwright see the frame navigate; time.sleep would not
        check(frame is not None, f"[{name}] the app frame reached its own page on {app_host}",
              [f.url for f in page.frames])
        if frame is None:
            return
        frame.wait_for_load_state("domcontentloaded")
        cookies = {(c["domain"], c["name"]): c["value"] for c in ctx.cookies()}
        desk_c = [v for (d, _n), v in cookies.items() if d.lstrip(".") == "home.cseries.localhost"]
        app_c = [v for (d, _n), v in cookies.items() if d.lstrip(".") == app_host]
        check(len(desk_c) == 1 and len(app_c) == 1 and desk_c != app_c,
              f"[{name}] the app origin holds a session cookie of its own, host-only", cookies)

        tok = frame.evaluate(f"""async () => {{
            const r = await fetch('/api/applications/{app_id}/frame-token',
                                  {{method: 'POST', credentials: 'same-origin'}});
            return [r.status, (await r.json().catch(() => ({{}}))).token ? 'token' : ''];
        }}""")
        check(tok == [200, "token"], f"[{name}] the frame gets its window token on its own origin", tok)

        forged = frame.evaluate("""async () => {
            const r = await fetch('/user-apps/', {headers: {'X-Forwarded-Host': 'home.cseries.localhost'}});
            return r.status; }""")
        check(forged == 404, f"[{name}] a page on the app origin cannot pass as the desktop with "
                             "X-Forwarded-Host", forged)

        csp = frame.evaluate("""async () => (await fetch(location.pathname))
                                 .headers.get('content-security-policy') || ''""")
        check(f"frame-ancestors {HOME}" in csp and "https://" not in csp,
              f"[{name}] the app page lets only the desktop, on its scheme and port, frame it", csp)

        renew = frame.evaluate("async () => (await fetch('/__host/renew')).status")
        check(renew == 204, f"[{name}] /__host/renew says the app origin's session is live", renew)

        out = page.evaluate("""async () => (await fetch('/api/owner/logout',
                                {method: 'POST', credentials: 'same-origin'})).status""")
        renew = frame.evaluate("async () => (await fetch('/__host/renew')).status")
        check(out == 200 and renew == 401,
              f"[{name}] signing out on the desktop ends the app origin's session at once", (out, renew))
        # The desktop's session-keeper, on a session that has ended: renew
        # says so, and its Continue (/__host/session) lands on the sign-in page.
        desk = page.evaluate("async () => (await fetch('/__host/renew')).status")
        page.goto(f"{HOME}/__host/session?rd=/", wait_until="domcontentloaded")
        check(desk == 401 and page.url == f"{HOME}/login",
              f"[{name}] after sign-out the desktop's renew is 401 and Continue lands on sign-in",
              (desk, page.url))
    finally:
        browser.close()


def main() -> int:
    if os.environ.get("BUILD") == "1":
        dc("build", capture=False)
    try:
        dc("up", "-d", capture=False)
        if not wait_healthy():
            print(dc("logs", "--tail=60", "host").stdout)
            check(False, "the Host answered its health check")
            return 1
        code = setup_code()
        req = urllib.request.Request(
            f"http://127.0.0.1:{PORT}/api/owner/setup",
            data=json.dumps({"code": code, "email": EMAIL, "password": PASSWORD}).encode(),
            headers={"Content-Type": "application/json", "Host": f"home.cseries.localhost:{PORT}",
                     "Origin": HOME}, method="POST")
        with urllib.request.urlopen(req, timeout=30) as r:
            check(r.status == 200, "owner created with the setup code from the log")

        found = in_host_python(r'''
import json, urllib.request
apps = json.load(urllib.request.urlopen("http://localhost:8800/api/applications"))["applications"]
iso = {a["id"]: a["origin_key"] for a in apps if a.get("isolated") and a.get("origin_key")}
app = "settings" if "settings" in iso else sorted(iso)[0]
print(app, iso[app])''').split()
        check(len(found) == 2, "an isolated app is installed", found)
        if len(found) != 2:
            return 1
        app_id, key = found

        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            opener.open(urllib.request.Request(
                f"http://127.0.0.1:{PORT}/system-apps/{app_id}/",
                headers={"Host": f"apps-{key}-home.cseries.localhost:{PORT}", "Accept": "text/html"}),
                timeout=10)
            where = ""
        except urllib.error.HTTPError as e:
            where = e.headers.get("location", "")
        check(where.startswith(f"{HOME}/__host/join?"),
              "an app origin with no session is sent to the desktop's join", where)

        try:
            opener.open(urllib.request.Request(
                f"http://127.0.0.1:{PORT}/__host/join?to=http://evil.example:{PORT}",
                headers={"Host": f"home.cseries.localhost:{PORT}"}), timeout=10)
            status = 200
        except urllib.error.HTTPError as e:
            status = e.code
        check(status in (400, 401, 403), "/__host/join never sends a code to a foreign origin", status)

        res = in_host_python(JOIN_UNIT % {"email": EMAIL, "password": PASSWORD}).split(",")
        for label, bit in zip(JOIN_LABELS, res + ["0"] * (len(JOIN_LABELS) - len(res))):
            check(bit == "1", label, res)

        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            for name in BROWSERS:
                browser_checks(pw, name, app_id, key)
    finally:
        if os.environ.get("KEEP") != "1":
            dc("down", "-v", "--remove-orphans")
    print("RESULT: ALL PASS" if not failed else f"RESULT: {failed} FAILED")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
