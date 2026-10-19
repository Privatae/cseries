#!/usr/bin/env python3
"""Gate: when the Host session ends, the sign-in prompt comes up on the first
request the router refuses, and a version card says the session ended — in
plain words — and loads again once it is back.

Measured 2026-10-06 (mid-demo): the session ended exactly an hour after
sign-in. From then on the router answered every request with 302 →
/__host/session → 302 → the sign-in site (the edge's access log). A page's
fetch() that follows a redirect to another origin rejects with TypeError
"Failed to fetch", so the Workbench's run card showed that in red (and so did
the Bench app's, since retired). The desktop's keep-alive (dashboard/base/js/session-keeper.js) asks
the router only every 5 minutes, and only a 401 from it raises the prompt — so
"Your session expired" appeared four minutes after the cards broke, and the
cards never recovered without a reload.

Driven in Chromium over a loopback mock that behaves like the router and the
edge do: while the session lives, /__host/renew answers 204 and the routes
answer; once it ends, every /api/* request is redirected to /__host/session,
which redirects to a sign-in stand-in on ANOTHER origin with no CORS — the
exact shape that makes fetch() reject — and /__host/renew answers 401 (what the
router answers for a dead cookie, and what the edge answers after a sign-out
the gate has processed). The desktop is a page that loads the real
session-keeper.js and frames the real BUILT Workbench (system-apps/workbench,
the book of pages); the isolated app is a page on a
second origin (localhost vs 127.0.0.1) that loads the real app-frame-init.js.

  D. the desktop's own request: the first one refused raises the prompt at
     once (not at the 5-minute ping); a failure that is NOT the session ending
     (the renewal says 204) raises nothing; a renewal that answers neither 204
     nor 401 (version skew) raises nothing; once the session is back, a
     tab-wake takes the prompt down;
  W. the Workbench run card's inline version (a run opened from Runs on the
     book's right side), and the version window's Changes: the plain sentence
     instead of "Failed to fetch", the desktop's prompt up at once, and both
     load by themselves when the session is back;
  I. an isolated app on its own origin: its first refused request raises the
     desktop's prompt at once.

Fail-before: APP_ROOT=<a tree without the change> python3 <this file> fails
every "at once", every sentence and every "loads again" check.

Needs Playwright + Chromium and free local ports — no pod, no DB. Run:

  APP_ROOT=<repo> python3 scripts/test_session_ended_first_failure.py
"""
from __future__ import annotations

import asyncio
import http.server
import json
import os
import re
import socket
import sys
import threading
import time
from pathlib import Path
from urllib.parse import quote

from playwright.sync_api import expect, sync_playwright

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
JS = ROOT / "dashboard" / "base" / "js"
WB_DIR = ROOT / "system-apps" / "workbench"
SHOTS = Path(os.environ.get("SHOTS_DIR", "/tmp/session-ended-first-failure"))
SHOTS.mkdir(parents=True, exist_ok=True)

ENDED_VERSION = "Your session ended — sign in again to load this version."
ENDED_CHANGES = "Your session ended — sign in again to load these changes."
AT_ONCE_MS = 3_000      # the keeper's own ping is every 5 minutes

fails = 0


def check(name, ok, detail=""):
    global fails
    print(("  ok    " if ok else "  FAIL  ") + name + (f"  — {detail}" if detail and not ok else ""))
    if not ok:
        fails += 1


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))       # loopback: the only client is this process's Chromium
        return s.getsockname()[1]


# ── the sign-in site: another origin, no CORS ───────────────────────────────
SIGNIN_HITS: list = []


class _SignIn(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        SIGNIN_HITS.append(self.path)
        body = b"<html><body>sign in</body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


SIGNIN_PORT = free_port()
_signin = http.server.ThreadingHTTPServer(("127.0.0.1", SIGNIN_PORT), _SignIn)
threading.Thread(target=_signin.serve_forever, daemon=True).start()

# ── the router + kernel, mocked ─────────────────────────────────────────────
from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse  # noqa: E402
import uvicorn  # noqa: E402
from gate_loopback import IDLE_KEEP_ALIVE_S  # noqa: E402

NOW = "2026-10-06T21:17:00Z"
STATE = {"alive": True, "renew": "router"}   # renew: "router" (204/401) | "skew" (404)
HOLD = {"pattern": None, "event": threading.Event(), "arrived": threading.Event()}
RENEWS: list = []
# Renewals and re-bootstraps the isolated origin (localhost:<port>) asked for
# itself, in arrival order: what one of its failed requests costs the gate.
ISO_SESSION_HITS: list = []
# Every version-card request (/preview/…), in arrival order: a card that
# reloads leaves a new one here.
PREVIEW_HITS: list = []

PAGE ="<!doctype html><html><body><h1 id='t'>the run's own version</h1></body></html>"
DIFF = "diff --git a/index.html b/index.html\n+<h1>the run's own version</h1>\n"
RUNS = {"sess-page": {"id": 802, "harness": "kimi"}}

app = FastAPI()


@app.middleware("http")
async def router_and_edge(request: Request, call_next):
    path = request.url.path
    # The app's own request that follows its redirect to the sign-in is not
    # counted (rd= its own path); only the frame-init's renewal and its
    # background re-bootstrap (rd=/__host/renew) are.
    if (request.headers.get("host") or "").startswith("localhost") and (
            path == "/__host/renew"
            or (path == "/__host/session" and request.query_params.get("rd") == "/__host/renew")):
        ISO_SESSION_HITS.append(path)
    if "/preview/" in path:
        PREVIEW_HITS.append(path)
    pat = HOLD["pattern"]
    if pat and re.search(pat, path):
        # Hold this one request until the test has ended the session, so the
        # card's version request is the first one refused — as it was live.
        HOLD["arrived"].set()
        await asyncio.to_thread(HOLD["event"].wait, 20)
    if path.startswith("/api/") and not STATE["alive"]:
        return RedirectResponse("/__host/session?rd=" + quote(path, safe=""), status_code=302)
    return await call_next(request)


@app.get("/__host/session")
def bootstrap(rd: str = "/"):
    return RedirectResponse(f"http://127.0.0.1:{SIGNIN_PORT}/application/o/authorize/?rd={quote(rd)}",
                            status_code=302)


@app.get("/__host/renew")
def renew(use: str = ""):
    # A real-use report (?use=1, sent when the browser records input — and a
    # Playwright evaluate counts as input) is not a probe; only probes count.
    if use != "1":
        RENEWS.append(time.time())
    if STATE["renew"] == "skew":
        return Response("not here", status_code=404)
    if STATE["alive"]:
        return Response(status_code=204, headers={"Cache-Control": "no-store"})
    return Response("session expired\n", status_code=401, headers={"Cache-Control": "no-store"})


@app.get("/api/not-the-session")
def not_the_session():
    # A failure that is not the session ending: a same-origin request that
    # still rejects (redirected off-origin) while the renewal says 204.
    return RedirectResponse(f"http://127.0.0.1:{SIGNIN_PORT}/elsewhere", status_code=302)


DESKTOP = """<!doctype html><html><head><meta charset="utf-8">
<style>:root{--cseries-night:#0b0f14;--cseries-mist:#334;--cseries-paper:#eee;--cseries-ink:#111}
body{margin:0;background:#000}iframe{border:0;width:1360px;height:880px}</style>
<script src="/js/session-keeper.js"></script></head>
<body><iframe id="app" src="%s"></iframe></body></html>"""


@app.get("/desktop")
def desktop(app: str = "about:blank"):
    return HTMLResponse(DESKTOP % app.replace('"', ""))


ISOLATED = """<!doctype html><html><head><meta charset="utf-8">
<script src="/js/app-frame-init.js" data-app-id="settings"></script></head>
<body><button id="go">load</button><pre id="out"></pre><script>
document.getElementById('go').onclick = function () {
  fetch('/api/settings/state').then(function (r) { out.textContent = 'ok ' + r.status; },
    function (e) { out.textContent = 'err ' + e.message; });
};</script></body></html>"""


@app.get("/isolated/index.html")
def isolated():
    return HTMLResponse(ISOLATED)


@app.get("/js/{name}")
def js(name: str):
    target = JS / name
    if not re.fullmatch(r"[a-z0-9-]+\.js", name) or not target.is_file():
        return Response(status_code=404)
    return FileResponse(str(target), media_type="text/javascript")


@app.get("/css/tokens.css")
def tokens():
    return Response(":root{}", media_type="text/css")


# Workbench routes (the set scripts/test_workbench_version_view.py mocks).
@app.get("/api/projects")
def list_projects(include_archived: bool = False, limit: int = 50):
    return {"projects": [{"id": 251, "name": "A site", "status": "active", "group": "active",
                          "bucket_id": None, "sort_order": None, "description": "", "phase_count": 0,
                          "repo": None, "last_activity_at": NOW}]}


@app.get("/api/projects/buckets")
def list_buckets():
    return {"buckets": [], "unfiled": {"counts": {}}}


@app.get("/api/projects/{pid}/modules")
def modules(pid: int):
    return {"project_id": pid, "root_app": None, "modules": [
        {"name": "theme", "target": "private_app", "validation": "review", "description": "",
         "current_version": {"number": 1, "run_id": 802, "commit": "858d531", "source": "build", "at": NOW},
         "version_count": 1, "app": None, "candidates": []}]}


@app.get("/api/projects/{pid}/modules/{mod}/versions")
def versions(pid: int, mod: str):
    return {"versions": []}


@app.get("/api/projects/{pid}/phases")
def phases(pid: int):
    return {"phases": []}


@app.get("/api/compute/runs")
def runs(project_id: int | None = None, window: str = "", limit: int = 40):
    return {"runs": [
        {"id": r["id"], "session_id": sid, "project_id": 251, "project_name": "A site",
         "harness": r["harness"], "model": None, "ok": True, "status": "active", "exit_code": 0,
         "has_error": False, "diff_bytes": 10, "elapsed_s": 30, "created_at": NOW}
        for sid, r in RUNS.items()]}


@app.get("/api/review/sessions")
def sessions(project_id: int | None = None, limit: int = 20):
    return {"sessions": []}


@app.get("/api/review/sessions/{sid}")
def session(sid: str):
    r = RUNS.get(sid)
    if not r:
        return JSONResponse({"detail": "session not found"}, status_code=404)
    return {"session_id": sid, "status": "complete",
            "manifest": {"harnesses": [r["harness"]], "project_id": 251, "prompt": "a page",
                         "module": "theme", "seeded": True},
            "entrypoints": {r["harness"]: True}}


@app.get("/api/review/sessions/{sid}/stream/{harness}")
async def stream(sid: str, harness: str, from_seq: int = 0):
    async def gen():
        if from_seq <= 1:
            yield json.dumps({"seq": 1, "kind": "text", "payload": {"text": f"{harness} working"},
                              "ts": time.time() - 60}) + "\n"
        if from_seq <= 2:
            yield json.dumps({"seq": 2, "kind": "status", "ts": time.time() - 30, "payload": {
                "state": "complete", "ok": True, "exit_code": 0, "elapsed_seconds": 30}}) + "\n"
    return StreamingResponse(gen(), media_type="application/x-ndjson")


@app.get("/api/review/sessions/{sid}/preview/{harness}")
def run_preview(sid: str, harness: str, entry: str | None = None):
    return HTMLResponse(PAGE)


@app.get("/api/review/sessions/{sid}/diff/{harness}")
def run_diff(sid: str, harness: str):
    return {"diff": DIFF}


@app.get("/api/applications")
def applications():
    return {"applications": [{"id": "workbench", "name": "Workbench"}]}


@app.get("/api/agents")
def agents(limit: int = 100, active_only: bool = False):
    return {"agents": []}


@app.get("/api/agents/scheduled")
def scheduled():
    return {"scheduled": []}


@app.get("/api/brain/chat")
def chat(app_id: str = "", project_id: int | None = None, order: str = "asc"):
    return {"turns": []}


@app.api_route("/api/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
def other_api(rest: str):
    return JSONResponse({"detail": "not mocked"}, status_code=404)


def _static(base: Path, rest: str):
    target = (base / rest).resolve()
    if base.resolve() not in target.parents:
        return Response(status_code=403)
    return FileResponse(str(target)) if target.is_file() else Response(status_code=404)


@app.get("/system-apps/workbench/{rest:path}")
def wb_static(rest: str):
    return _static(WB_DIR, rest)


def serve() -> int:
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error",
                                           timeout_keep_alive=IDLE_KEEP_ALIVE_S))
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    return port


# ── helpers ─────────────────────────────────────────────────────────────────
def alive(on: bool):
    STATE["alive"] = on


def hold(pattern: str | None):
    HOLD["event"].clear()
    HOLD["arrived"].clear()
    HOLD["pattern"] = pattern


def release():
    HOLD["pattern"] = None
    HOLD["event"].set()


def overlay(page):
    return page.locator("#session-expired-overlay")


def overlay_within(page, ms) -> bool:
    try:
        expect(overlay(page)).to_be_visible(timeout=ms)
        return True
    except AssertionError:
        return False


def tab_wake(page):
    """What a person coming back to the tab does: the keeper's tab-wake ping."""
    page.evaluate("() => document.dispatchEvent(new Event('visibilitychange'))")


def forged_back_reloads(page, app_path: str) -> int:
    """While the session is still gone, the app's own frame posts the desktop's
    'session back' message to itself: a message that did not come from the
    desktop (an isolated app can do exactly this to any frame it can reach).
    Returns how many version-card requests followed it; it must be none."""
    # The app's own frame — not the desktop, whose URL carries ?app=<app_path>.
    frame = next(f for f in page.frames
                 if f is not page.main_frame and app_path in f.url.split("?", 1)[0])
    before = len(PREVIEW_HITS)
    frame.evaluate("() => window.postMessage({ type: 'cseries:session', state: 'ok' }, '*')")
    page.wait_for_timeout(1_500)
    return len(PREVIEW_HITS) - before


def end_session_while(page, pattern):
    """End the session while the request matching `pattern` is in flight."""
    ok = HOLD["arrived"].wait(10)
    check(f"(setup) the request {pattern!r} reached the mock", ok)
    alive(False)
    release()


# ── scenarios ───────────────────────────────────────────────────────────────
def scenario_desktop(page, base):
    print("D. the desktop's own request")
    alive(True)
    STATE["renew"] = "router"
    page.goto(f"{base}/desktop", wait_until="load")
    page.wait_for_timeout(300)
    alive(False)
    page.evaluate("() => { fetch('/api/tickets').catch(() => {}); }")
    check("D: the first refused request raises the prompt at once", overlay_within(page, AT_ONCE_MS))
    alive(True)
    tab_wake(page)
    try:
        expect(overlay(page)).to_have_count(0, timeout=AT_ONCE_MS)
        down = True
    except AssertionError:
        down = False
    check("D: once the session is back, a tab-wake takes the prompt down", down)

    page.goto(f"{base}/desktop", wait_until="load")
    page.wait_for_timeout(300)
    n = len(RENEWS)
    page.evaluate("() => { fetch('/api/not-the-session').catch(() => {}); }")
    page.wait_for_timeout(3_000)
    check("D: a failed request is checked against the renewal (one probe)", len(RENEWS) == n + 1,
          f"{len(RENEWS) - n} renewals")
    check("D: ... and when the renewal says 204 no prompt comes up", overlay(page).count() == 0)

    page.goto(f"{base}/desktop", wait_until="load")
    page.wait_for_timeout(300)
    STATE["renew"] = "skew"
    alive(False)
    page.evaluate("() => { fetch('/api/tickets').catch(() => {}); }")
    page.wait_for_timeout(1_500)
    check("D: a renewal that answers neither 204 nor 401 (version skew) raises nothing",
          overlay(page).count() == 0)
    STATE["renew"] = "router"
    alive(True)


def open_wb_run(page, base):
    page.goto(f"{base}/desktop?app=/system-apps/workbench/index.html", wait_until="load")
    wb = page.frame_locator("#app")
    # The book's right side: "This page", then the bench's sections.
    bench = wb.locator("aside.book-right")
    expect(bench.locator("[data-module='theme']")).to_be_visible(timeout=10_000)
    bench.locator("button[data-state]").filter(has_text=re.compile(r"^\s*Runs")).first.click()
    bench.locator("button").filter(has_text="#802").first.click()
    return wb, bench.locator("[data-run-card][data-harness='kimi']").first


def scenario_workbench(page, base):
    print("W. the Workbench run card and its version window")
    alive(True)
    hold(r"/preview/kimi$")
    wb, card = open_wb_run(page, base)
    end_session_while(page, "/preview/kimi")
    t0 = time.time()
    shown = overlay_within(page, AT_ONCE_MS)
    check("W: the desktop's prompt is up at once after the card's request is refused", shown,
          f"{time.time() - t0:.1f}s")
    msg = card.locator("[data-run-preview] [data-version-error]")
    try:
        expect(msg).to_have_count(1, timeout=AT_ONCE_MS)
    except AssertionError:
        pass
    text = card.locator("[data-run-preview]").inner_text(timeout=2_000) if card.locator("[data-run-preview]").count() else ""
    check("W: the card says the session ended, in plain words", ENDED_VERSION in text, repr(text[:120]))
    check("W: ... never the browser's \"Failed to fetch\"", "Failed to fetch" not in text, repr(text[:120]))
    page.screenshot(path=str(SHOTS / "W-ended.png"))
    n = forged_back_reloads(page, "/system-apps/workbench/")
    check("W: a 'session back' message that is not the desktop's does not reload the card",
          n == 0, f"{n} card request(s) followed it")
    alive(True)
    tab_wake(page)
    try:
        expect(card.frame_locator("[data-run-preview] iframe").locator("h1")).to_have_text(
            "the run's own version", timeout=6_000)
        back = True
    except AssertionError:
        back = False
    check("W: once the session is back, the card loads the version by itself", back)
    check("W: ... and the prompt is down", overlay(page).count() == 0)

    # The version window's Changes (RunDiff).
    card.locator("[data-open-version]").click()
    vv = wb.locator("[data-testid='version-view']")
    expect(vv).to_be_visible(timeout=5_000)
    hold(r"/diff/kimi$")
    vv.locator("button", has_text="Changes").click()
    end_session_while(page, "/diff/kimi")
    check("W: the prompt is up at once after the diff request is refused", overlay_within(page, AT_ONCE_MS))
    try:
        expect(vv).to_contain_text(ENDED_CHANGES, timeout=AT_ONCE_MS)
        said = True
    except AssertionError:
        said = False
    check("W: Changes says the session ended, in plain words", said, repr(vv.inner_text()[:160]))
    check("W: ... never \"Failed to fetch\"", "Failed to fetch" not in vv.inner_text())
    alive(True)
    tab_wake(page)
    try:
        expect(vv).to_contain_text("the run's own version", timeout=6_000)
        back = True
    except AssertionError:
        back = False
    check("W: once the session is back, Changes loads the diff by itself", back)


def scenario_isolated(page, base, other):
    print("I. an isolated app on its own origin")
    alive(True)
    page.goto(f"{base}/desktop?app={other}/isolated/index.html", wait_until="load")
    frame = page.frame_locator("#app")
    expect(frame.locator("#go")).to_be_visible(timeout=5_000)
    page.wait_for_timeout(300)
    alive(False)
    frame.locator("#go").click()
    t0 = time.time()
    check("I: the app's first refused request raises the desktop's prompt at once",
          overlay_within(page, AT_ONCE_MS), f"{time.time() - t0:.1f}s")
    page.wait_for_timeout(500)
    leaked = [h for h in SIGNIN_HITS if "__host" in h and "renew" in h]
    check("I: no background renewal or re-bootstrap reaches the sign-in site (manual redirects only)",
          not leaked, repr(leaked[:3]))
    # Once this origin knows the session is gone, its later failed requests ask
    # nothing more: behind the edge gate every renewal and re-bootstrap is one
    # more outpost check, and an app that polls keeps failing until Continue.
    # Spaced past the 2 s probe gap, so only the "already ended" guard can pass.
    before = len(ISO_SESSION_HITS)
    for _ in range(2):
        page.wait_for_timeout(2_300)
        frame.locator("#go").dispatch_event("click")   # the overlay covers the frame now
    page.wait_for_timeout(800)
    extra = ISO_SESSION_HITS[before:]
    check("I: after the session is known ended, a failed request costs no renewal or re-bootstrap",
          not extra, f"{len(extra)} extra: {extra[:6]}")
    # A tab-wake still asks (it is how a session restored elsewhere is noticed).
    before = len(ISO_SESSION_HITS)
    frame.locator("body").evaluate("""() => {
        Object.defineProperty(document, 'visibilityState', {value: 'visible', configurable: true});
        document.dispatchEvent(new Event('visibilitychange'));
    }""")
    page.wait_for_timeout(1_500)
    check("I: a tab-wake still asks the renewal after the session ended",
          "/__host/renew" in ISO_SESSION_HITS[before:], repr(ISO_SESSION_HITS[before:]))
    alive(True)


def main() -> int:
    port = serve()
    base = f"http://127.0.0.1:{port}"
    other = f"http://localhost:{port}"     # a second origin, the isolated app's
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            for name, fn in (("D", lambda pg: scenario_desktop(pg, base)),
                             ("W", lambda pg: scenario_workbench(pg, base)),
                             ("I", lambda pg: scenario_isolated(pg, base, other))):
                ctx = browser.new_context(viewport={"width": 1400, "height": 920})
                page = ctx.new_page()
                try:
                    fn(page)
                except Exception as e:  # a scenario that cannot finish is a failure, not a crash
                    check(f"{name}: scenario ran to the end", False, f"{type(e).__name__}: {str(e)[:300]}")
                    page.screenshot(path=str(SHOTS / f"{name}-crash.png"))
                finally:
                    release()
                    alive(True)
                    ctx.close()
        finally:
            browser.close()
    print()
    print("FAILED" if fails else "PASS", f"({fails} failing check(s))")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
